#!/usr/bin/env python3
"""
org_tagger.py

在 AWS Organizations 管理账号（Payer）上运行：
  列出组织内所有 ACTIVE 账号 → assume role 进每个账号 → 按 region 枚举资源
  → 给缺少指定标签的资源补上标签 → 输出 CSV 报告。

默认 dry-run（只出报告，不改任何东西）；确认报告无误后加 --apply 才真正写标签。

示例：
  # 1) 挑一个账号、一个 region 试跑
  python org_tagger.py --tag-key map-migrated --tag-value migXXXXXXXXXX \
      --accounts 111122223333 --regions ap-southeast-1

  # 2) 全组织 dry-run
  python org_tagger.py --tag-key map-migrated --tag-value migXXXXXXXXXX

  # 3) 审完报告后执行
  python org_tagger.py --tag-key map-migrated --tag-value migXXXXXXXXXX --apply

说明：
  - 仅供参考，请先在测试账号验证后再用于生产。
  - 只支持 aws 标准分区，不含中国区（aws-cn）。
  - 管理账号自身用当前凭证处理；成员账号通过 --role-name 指定的角色进入。
"""
import argparse
import csv
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Dict

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

BOTO_CFG = Config(retries={"max_attempts": 10, "mode": "adaptive"})
HOME_REGION = boto3.Session().region_name or "us-east-1"
log = logging.getLogger("org_tagger")


# ----------------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------------

@dataclass
class Res:
    rtype: str                      # 报告里显示的资源类型，如 ec2:instance
    rid: str                        # 资源 ID 或 ARN
    tags: Dict[str, str]            # 现有标签
    apply: Callable[[], None]       # 执行打标签的函数
    from_sweep: bool = False        # 是否来自 Tagging API 补扫


def tdict(tags):
    """[{'Key':..,'Value':..}] -> {key: value}"""
    return {t["Key"]: t.get("Value", "") for t in (tags or [])}


def paginate(client, method, result_key, **kwargs):
    """有分页器就用分页器，没有就单次调用。"""
    if client.can_paginate(method):
        for page in client.get_paginator(method).paginate(**kwargs):
            yield from page.get(result_key, []) or []
    else:
        yield from getattr(client, method)(**kwargs).get(result_key, []) or []


def arn_tail(arn):
    return arn.split(":")[-1].split("/")[-1]


def rgt_tag(rgt, arns, tags):
    """用 Resource Groups Tagging API 打标签；部分失败时抛异常，让报告记成 FAILED。"""
    resp = rgt.tag_resources(ResourceARNList=arns, Tags=tags)
    failed = resp.get("FailedResourcesMap") or {}
    if failed:
        raise RuntimeError("; ".join(
            f"{a}: {v.get('ErrorCode')} {v.get('ErrorMessage')}" for a, v in failed.items()))


class Ctx:
    """一个账号 + 一个 region 的上下文，缓存 client。只在单线程里使用。"""

    def __init__(self, sess, region, key, value):
        self.sess, self.region, self.key, self.value = sess, region, key, value
        self._clients = {}

    def client(self, service, region=None):
        r = region or self.region
        if (service, r) not in self._clients:
            self._clients[(service, r)] = self.sess.client(service, region_name=r, config=BOTO_CFG)
        return self._clients[(service, r)]

    def tagset(self):
        return [{"Key": self.key, "Value": self.value}]

    def tagdict(self):
        return {self.key: self.value}


def get_session(account_id, mgmt_id, role_name):
    """管理账号直接用当前凭证；成员账号 assume role。
    每个 region 都重新取一次凭证，避免长时间运行时 1 小时凭证过期。"""
    if account_id == mgmt_id:
        return boto3.Session()
    sts = boto3.Session().client("sts", region_name=HOME_REGION, config=BOTO_CFG)
    c = sts.assume_role(
        RoleArn=f"arn:aws:iam::{account_id}:role/{role_name}",
        RoleSessionName="org-tagger",
    )["Credentials"]
    return boto3.Session(
        aws_access_key_id=c["AccessKeyId"],
        aws_secret_access_key=c["SecretAccessKey"],
        aws_session_token=c["SessionToken"],
    )


# ----------------------------------------------------------------------------
# 资源枚举器（第一层：按服务原生 API 枚举，能找到“从没打过标签”的资源）
# 想覆盖更多类型，照着下面的格式加函数，再放进 REGIONAL_HANDLERS / GLOBAL_HANDLERS
# ----------------------------------------------------------------------------

def _ec2(ctx, label, items, id_field):
    ec2 = ctx.client("ec2")
    for it in items:
        rid = it[id_field]
        yield Res(label, rid, tdict(it.get("Tags")),
                  lambda rid=rid: ec2.create_tags(Resources=[rid], Tags=ctx.tagset()))


def h_ec2_instances(ctx):
    items = (i for r in paginate(ctx.client("ec2"), "describe_instances", "Reservations")
             for i in r["Instances"]
             if i["State"]["Name"] not in ("terminated", "shutting-down"))
    yield from _ec2(ctx, "ec2:instance", items, "InstanceId")


def h_ec2_volumes(ctx):
    yield from _ec2(ctx, "ec2:volume",
                    paginate(ctx.client("ec2"), "describe_volumes", "Volumes"), "VolumeId")


def h_ec2_snapshots(ctx):
    yield from _ec2(ctx, "ec2:snapshot",
                    paginate(ctx.client("ec2"), "describe_snapshots", "Snapshots", OwnerIds=["self"]),
                    "SnapshotId")


def h_ec2_images(ctx):
    yield from _ec2(ctx, "ec2:image",
                    paginate(ctx.client("ec2"), "describe_images", "Images", Owners=["self"]),
                    "ImageId")


def h_ec2_eips(ctx):
    items = (a for a in paginate(ctx.client("ec2"), "describe_addresses", "Addresses")
             if "AllocationId" in a)
    yield from _ec2(ctx, "ec2:elastic-ip", items, "AllocationId")


def h_ec2_natgw(ctx):
    items = (n for n in paginate(ctx.client("ec2"), "describe_nat_gateways", "NatGateways")
             if n.get("State") in ("pending", "available"))
    yield from _ec2(ctx, "ec2:natgateway", items, "NatGatewayId")


def h_ec2_vpce(ctx):
    items = (e for e in paginate(ctx.client("ec2"), "describe_vpc_endpoints", "VpcEndpoints")
             if str(e.get("State", "")).lower() not in
             ("deleting", "deleted", "rejected", "failed", "expired"))
    yield from _ec2(ctx, "ec2:vpc-endpoint", items, "VpcEndpointId")


def _rds(ctx, label, items, arn_field):
    rds = ctx.client("rds")
    for it in items:
        arn = it[arn_field]
        yield Res(label, arn, tdict(it.get("TagList")),
                  lambda arn=arn: rds.add_tags_to_resource(ResourceName=arn, Tags=ctx.tagset()))


def h_rds_instances(ctx):
    yield from _rds(ctx, "rds:db",
                    paginate(ctx.client("rds"), "describe_db_instances", "DBInstances"),
                    "DBInstanceArn")


def h_rds_clusters(ctx):
    yield from _rds(ctx, "rds:cluster",
                    paginate(ctx.client("rds"), "describe_db_clusters", "DBClusters"),
                    "DBClusterArn")


def h_rds_snapshots(ctx):
    yield from _rds(ctx, "rds:snapshot",
                    paginate(ctx.client("rds"), "describe_db_snapshots", "DBSnapshots",
                             SnapshotType="manual"),
                    "DBSnapshotArn")


def h_rds_cluster_snapshots(ctx):
    yield from _rds(ctx, "rds:cluster-snapshot",
                    paginate(ctx.client("rds"), "describe_db_cluster_snapshots", "DBClusterSnapshots",
                             SnapshotType="manual"),
                    "DBClusterSnapshotArn")


def h_lambda(ctx):
    lam = ctx.client("lambda")
    for f in paginate(lam, "list_functions", "Functions"):
        arn = f["FunctionArn"]
        tags = lam.list_tags(Resource=arn).get("Tags", {}) or {}
        yield Res("lambda:function", arn, tags,
                  lambda arn=arn: lam.tag_resource(Resource=arn, Tags=ctx.tagdict()))


def h_elbv2(ctx):
    elb = ctx.client("elbv2")
    arns = [lb["LoadBalancerArn"] for lb in paginate(elb, "describe_load_balancers", "LoadBalancers")]
    for i in range(0, len(arns), 20):
        for td in elb.describe_tags(ResourceArns=arns[i:i + 20])["TagDescriptions"]:
            arn = td["ResourceArn"]
            yield Res("elbv2:loadbalancer", arn, tdict(td.get("Tags")),
                      lambda arn=arn: elb.add_tags(ResourceArns=[arn], Tags=ctx.tagset()))


def h_dynamodb(ctx):
    ddb = ctx.client("dynamodb")
    for name in paginate(ddb, "list_tables", "TableNames"):
        arn = ddb.describe_table(TableName=name)["Table"]["TableArn"]
        tags = tdict(paginate(ddb, "list_tags_of_resource", "Tags", ResourceArn=arn))
        yield Res("dynamodb:table", arn, tags,
                  lambda arn=arn: ddb.tag_resource(ResourceArn=arn, Tags=ctx.tagset()))


def h_efs(ctx):
    efs = ctx.client("efs")
    for fs in paginate(efs, "describe_file_systems", "FileSystems"):
        fid = fs["FileSystemId"]
        arn = fs.get("FileSystemArn") or fid
        yield Res("efs:file-system", arn, tdict(fs.get("Tags")),
                  lambda fid=fid: efs.tag_resource(ResourceId=fid, Tags=ctx.tagset()))


def h_elasticache(ctx):
    ec = ctx.client("elasticache")
    for c in paginate(ec, "describe_cache_clusters", "CacheClusters"):
        arn = c["ARN"]
        tags = tdict(ec.list_tags_for_resource(ResourceName=arn).get("TagList"))
        yield Res("elasticache:cluster", arn, tags,
                  lambda arn=arn: ec.add_tags_to_resource(ResourceName=arn, Tags=ctx.tagset()))


def h_eks(ctx):
    eks = ctx.client("eks")
    for name in paginate(eks, "list_clusters", "clusters"):
        c = eks.describe_cluster(name=name)["cluster"]
        arn = c["arn"]
        yield Res("eks:cluster", arn, c.get("tags") or {},
                  lambda arn=arn: eks.tag_resource(resourceArn=arn, tags=ctx.tagdict()))


# ---- 全局资源（每个账号只跑一次）----

def h_s3(ctx):
    s3 = ctx.client("s3", "us-east-1")
    for b in paginate(s3, "list_buckets", "Buckets"):
        name = b["Name"]
        loc = b.get("BucketRegion")
        if not loc:
            loc = s3.get_bucket_location(Bucket=name).get("LocationConstraint") or "us-east-1"
            if loc == "EU":
                loc = "eu-west-1"
        try:
            tags = tdict(ctx.client("s3", loc).get_bucket_tagging(Bucket=name)["TagSet"])
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchTagSet":
                raise
            tags = {}
        arn = f"arn:aws:s3:::{name}"
        # 不直接用 PutBucketTagging：它会整体覆盖桶的标签集，
        # 而且 CloudFormation 建的桶带 aws: 前缀标签，PutBucketTagging 写不回去。
        # Tagging API 是“追加”语义，在桶所在 region 调用。
        yield Res("s3:bucket", arn, tags,
                  lambda arn=arn, loc=loc: rgt_tag(ctx.client("resourcegroupstaggingapi", loc),
                                                   [arn], ctx.tagdict()))


def h_cloudfront(ctx):
    cf = ctx.client("cloudfront", "us-east-1")
    for page in cf.get_paginator("list_distributions").paginate():
        for d in (page.get("DistributionList", {}) or {}).get("Items", []) or []:
            arn = d["ARN"]
            tags = tdict(cf.list_tags_for_resource(Resource=arn)["Tags"].get("Items"))
            yield Res("cloudfront:distribution", arn, tags,
                      lambda arn=arn: cf.tag_resource(Resource=arn, Tags={"Items": ctx.tagset()}))


# ----------------------------------------------------------------------------
# 第二层：Tagging API 补扫
# GetResources 只返回“当前有标签或曾经打过标签”的资源，找不到从没打过标签的，
# 但能覆盖第一层没写到的各种资源类型（SNS、SQS、KMS、日志组……）。
# ----------------------------------------------------------------------------

def h_sweep(ctx):
    rgt = ctx.client("resourcegroupstaggingapi")
    for m in paginate(rgt, "get_resources", "ResourceTagMappingList"):
        arn = m["ResourceARN"]
        svc = arn.split(":")[2]
        yield Res(f"{svc} (sweep)", arn, tdict(m.get("Tags")),
                  lambda arn=arn: rgt_tag(rgt, [arn], ctx.tagdict()), from_sweep=True)


GLOBAL_HANDLERS = [h_s3, h_cloudfront]
REGIONAL_HANDLERS = [
    h_ec2_instances, h_ec2_volumes, h_ec2_snapshots, h_ec2_images,
    h_ec2_eips, h_ec2_natgw, h_ec2_vpce,
    h_rds_instances, h_rds_clusters, h_rds_snapshots, h_rds_cluster_snapshots,
    h_lambda, h_elbv2, h_dynamodb, h_efs, h_elasticache, h_eks,
]


# ----------------------------------------------------------------------------
# 报告
# ----------------------------------------------------------------------------

class Report:
    FIELDS = ["account_id", "account_name", "region", "type", "resource",
              "current_value", "action", "detail"]

    def __init__(self, path):
        # utf-8-sig：Excel 直接打开中文不乱码
        self.f = open(path, "w", newline="", encoding="utf-8-sig")
        self.w = csv.writer(self.f)
        self.w.writerow(self.FIELDS)
        self.lock = threading.Lock()
        self.counts = {}

    def row(self, account_id, account_name, region, rtype, resource,
            current_value, action, detail=""):
        with self.lock:
            self.w.writerow([account_id, account_name, region, rtype, resource,
                             current_value or "", action, detail])
            self.f.flush()
            self.counts[action] = self.counts.get(action, 0) + 1

    def close(self):
        self.f.close()


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------

def list_accounts(org):
    out = []
    for a in paginate(org, "list_accounts", "Accounts"):
        state = a.get("State") or a.get("Status")   # 新字段 State，旧字段 Status
        if state == "ACTIVE":
            out.append((a["Id"], a.get("Name", "")))
    return out


def enabled_regions(sess):
    ec2 = sess.client("ec2", region_name=HOME_REGION, config=BOTO_CFG)
    # 不带 AllRegions 参数时，只返回这个账号已启用的 region
    return sorted(r["RegionName"] for r in ec2.describe_regions()["Regions"])


def process_account(acct_id, acct_name, mgmt_id, args, report):
    seen = set()   # 去重：第一层处理过的资源，第二层补扫时跳过

    def run(handlers, region, region_label):
        sess = get_session(acct_id, mgmt_id, args.role_name)
        ctx = Ctx(sess, region, args.tag_key, args.tag_value)
        for h in handlers:
            try:
                for res in h(ctx):
                    if res.rid in seen or (res.from_sweep and arn_tail(res.rid) in seen):
                        continue
                    seen.add(res.rid)
                    cur = res.tags.get(args.tag_key)
                    base = (acct_id, acct_name, region_label, res.rtype, res.rid, cur)
                    if cur == args.tag_value:
                        report.row(*base, "OK")
                    elif cur is not None and not args.overwrite:
                        report.row(*base, "CONFLICT", "已有同名 key 但值不同；加 --overwrite 才覆盖")
                    elif not args.apply:
                        report.row(*base, "WOULD_TAG")
                    else:
                        try:
                            res.apply()
                            report.row(*base, "TAGGED")
                        except Exception as e:  # noqa: BLE001
                            report.row(*base, "FAILED", str(e)[:500])
            except Exception as e:  # noqa: BLE001
                # 某个服务在该 region 不可用 / 没权限 / 被 SCP 拒绝，记下来继续跑
                report.row(acct_id, acct_name, region_label, h.__name__, "", "", "ERROR", str(e)[:500])

    try:
        sess = get_session(acct_id, mgmt_id, args.role_name)
        regions = args.regions or enabled_regions(sess)
    except Exception as e:  # noqa: BLE001
        report.row(acct_id, acct_name, "-", "assume_role", "", "", "ERROR", str(e)[:500])
        return

    run(GLOBAL_HANDLERS, "us-east-1", "global")
    regional = REGIONAL_HANDLERS + ([] if args.no_sweep else [h_sweep])
    for region in regions:
        log.info("[%s] %s", acct_id, region)
        run(regional, region, region)


def main():
    p = argparse.ArgumentParser(description="从 Organizations 管理账号给全组织资源补标签")
    p.add_argument("--tag-key", required=True)
    p.add_argument("--tag-value", required=True)
    p.add_argument("--role-name", default="OrganizationAccountAccessRole",
                   help="成员账号里被 assume 的角色名（默认 OrganizationAccountAccessRole）")
    p.add_argument("--accounts", nargs="*", help="只处理这些账号 ID（默认全部 ACTIVE 账号）")
    p.add_argument("--exclude-accounts", nargs="*", default=[], help="跳过这些账号 ID")
    p.add_argument("--regions", nargs="*", help="只处理这些 region（默认每个账号已启用的全部 region）")
    p.add_argument("--no-sweep", action="store_true", help="不跑 Tagging API 补扫，只处理第一层列出的类型")
    p.add_argument("--overwrite", action="store_true", help="已有同名 key 但值不同时覆盖")
    p.add_argument("--apply", action="store_true", help="真正打标签；不加则只 dry-run")
    p.add_argument("--workers", type=int, default=8, help="并行处理的账号数")
    p.add_argument("--output", default="tagging_report.csv")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("botocore").setLevel(logging.WARNING)

    base = boto3.Session()
    mgmt_id = base.client("sts", region_name=HOME_REGION).get_caller_identity()["Account"]
    org = base.client("organizations", region_name=HOME_REGION, config=BOTO_CFG)

    accounts = list_accounts(org)
    if args.accounts:
        wanted = set(args.accounts)
        accounts = [a for a in accounts if a[0] in wanted]
    excluded = set(args.exclude_accounts)
    accounts = [a for a in accounts if a[0] not in excluded]

    log.info("模式: %s | 标签: %s=%s | 账号数: %d",
             "APPLY" if args.apply else "DRY-RUN", args.tag_key, args.tag_value, len(accounts))

    report = Report(args.output)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_account, aid, name, mgmt_id, args, report): aid
                for aid, name in accounts}
        for f in as_completed(futs):
            aid = futs[f]
            try:
                f.result()
                log.info("完成账号 %s", aid)
            except Exception as e:  # noqa: BLE001
                log.error("账号 %s 异常: %s", aid, e)
    report.close()
    log.info("统计: %s", report.counts)
    log.info("报告已写入: %s", args.output)


if __name__ == "__main__":
    main()
