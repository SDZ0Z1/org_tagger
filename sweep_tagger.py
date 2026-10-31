#!/usr/bin/env python3
"""
sweep_tagger.py

部署在 Payer（管理账号）的 Lambda 函数，每天由 EventBridge Scheduler 触发一次，
给组织里各成员账号缺少标签的资源补上，兜住实时层（realtime_tagger.py）漏掉的。

每个账号打什么值，来自 SSM 参数里的账号映射表，例如：
  {"*": "migXXXXXXXXXX", "444455556666": "migYYYYYYYYYY", "777788889999": ""}
  "*"         所有 ACTIVE 成员账号的默认值（从 Organizations 列出，不含 Payer 自己）
  12 位账号 ID  这个账号用自己的值；值为空表示排除这个账号
Payer 自己只有在表里单独写一行时才处理。

一次运行由同一个函数的两种调用组成：
  调度：Scheduler 触发，事件里没有 account。按映射表算出要处理的账号，确认每个账号能 assume 进
        MapTaggerRole、取得已启用的 region，再按“账号 × region”异步调用本函数自己，另外每个账号调用一次 global。
  扫描：事件里有 account 和 region。assume 进该账号的 MapTaggerRole，枚举这个 region 的资源，
        缺标签的补上；region 为 global 时处理 S3 和 CloudFront。
拆开是因为 Lambda 单次调用最长 15 分钟，整个组织放在一次调用里可能跑不完。

环境变量：
  MAPPING_PARAM  映射表所在的 SSM 参数名，默认 /map-tagger/accounts
  ROLE_NAME      各账号里被 assume 的角色名，默认 MapTaggerRole
  TAG_KEY        标签 key，默认 map-migrated
  MODE           dryrun（默认，只写日志）或 apply（真正打标签）
  OVERWRITE      true 时覆盖已有的不同值；默认 false，记为 CONFLICT
  REGIONS        只处理这些 region，逗号分隔；默认每个账号已启用的全部 region
  NO_SWEEP       true 时不跑第二层 Tagging API 补扫，只处理第一层列出的类型

每处理一个资源，往 CloudWatch Logs 写一行 JSON，字段和 realtime_tagger.py 一致。

在控制台测试单个账号、单个 region，用这样的测试事件：
  {"account": "111122223333", "region": "ap-southeast-1"}

说明：
  - 仅供参考，请先用 MODE=dryrun 验证后再改成 apply。
  - 只支持 aws 标准分区，不含中国区（aws-cn）。
  - Payer 自己的资源也要扫描时，在 Payer 里同样建 MapTaggerRole，并把 Payer 单独写进映射表。
"""
import json
import os
import socket
from dataclasses import dataclass
from typing import Callable, Dict
from urllib.parse import urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

BOTO_CFG = Config(retries={"max_attempts": 10, "mode": "adaptive"})
REGION = os.environ.get("AWS_REGION", "us-east-1")
MAPPING_PARAM = os.environ.get("MAPPING_PARAM", "/map-tagger/accounts")
ROLE_NAME = os.environ.get("ROLE_NAME", "MapTaggerRole")
TAG_KEY = os.environ.get("TAG_KEY", "map-migrated")
APPLY = os.environ.get("MODE", "dryrun").lower() == "apply"
OVERWRITE = os.environ.get("OVERWRITE", "false").lower() == "true"
REGIONS = [r.strip() for r in os.environ.get("REGIONS", "").split(",") if r.strip()]
NO_SWEEP = os.environ.get("NO_SWEEP", "false").lower() == "true"


# ----------------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------------

@dataclass
class Res:
    rtype: str                      # 日志里显示的资源类型，如 ec2:instance
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
    """用 Resource Groups Tagging API 打标签；部分失败时抛异常，让日志记成 FAILED。"""
    resp = rgt.tag_resources(ResourceARNList=arns, Tags=tags)
    failed = resp.get("FailedResourcesMap") or {}
    if failed:
        raise RuntimeError("; ".join(
            f"{a}: {v.get('ErrorCode')} {v.get('ErrorMessage')}" for a, v in failed.items()))


def log(**fields):
    print(json.dumps(fields, ensure_ascii=False, default=str))


class ServiceUnavailable(Exception):
    """这个服务在该 region 没有接口（服务没在这个 region 上线）。"""


class Ctx:
    """一个账号 + 一个 region 的上下文，缓存 client。"""

    def __init__(self, sess, region, key, value):
        self.sess, self.region, self.key, self.value = sess, region, key, value
        self._clients = {}

    def client(self, service, region=None):
        r = region or self.region
        if (service, r) not in self._clients:
            c = self.sess.client(service, region_name=r, config=BOTO_CFG)
            # 服务没在这个 region 上线时接口域名解析不了；先查一下，免得 botocore 反复重试连接。
            # 解析失败在本地是 gaierror，在 Lambda 里可能是 [Errno 16] Device or resource busy，所以接 OSError。
            # botocore 自带的数据也认为这个 region 有该服务时，多半是 DNS 临时出错，照常报错
            try:
                socket.getaddrinfo(urlparse(c.meta.endpoint_url).hostname, 443)
            except OSError:
                if r in self.sess.get_available_regions(service):
                    raise
                raise ServiceUnavailable(f"{service} 在 {r} 没有接口")
            self._clients[(service, r)] = c
        return self._clients[(service, r)]

    def tagset(self):
        return [{"Key": self.key, "Value": self.value}]

    def tagdict(self):
        return {self.key: self.value}


def get_session(account_id):
    """assume 进该账号的 MapTaggerRole。Payer 自己也走这条路，权限和成员账号一致。"""
    sts = boto3.client("sts", region_name=REGION, config=BOTO_CFG)
    c = sts.assume_role(
        RoleArn=f"arn:aws:iam::{account_id}:role/{ROLE_NAME}",
        RoleSessionName="map-daily-sweep",
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


def h_elb_classic(ctx):
    elb = ctx.client("elb")
    names = [lb["LoadBalancerName"] for lb in paginate(elb, "describe_load_balancers", "LoadBalancerDescriptions")]
    for i in range(0, len(names), 20):
        for td in elb.describe_tags(LoadBalancerNames=names[i:i + 20])["TagDescriptions"]:
            name = td["LoadBalancerName"]
            yield Res("elb:loadbalancer", name, tdict(td.get("Tags")),
                      lambda name=name: elb.add_tags(LoadBalancerNames=[name], Tags=ctx.tagset()))


def h_sqs(ctx):
    sqs = ctx.client("sqs")
    for url in paginate(sqs, "list_queues", "QueueUrls"):
        acct, name = url.rstrip("/").split("/")[-2:]
        tags = sqs.list_queue_tags(QueueUrl=url).get("Tags", {}) or {}
        yield Res("sqs:queue", f"arn:aws:sqs:{ctx.region}:{acct}:{name}", tags,
                  lambda url=url: sqs.tag_queue(QueueUrl=url, Tags=ctx.tagdict()))


def h_sns(ctx):
    sns = ctx.client("sns")
    for t in paginate(sns, "list_topics", "Topics"):
        arn = t["TopicArn"]
        tags = tdict(sns.list_tags_for_resource(ResourceArn=arn).get("Tags"))
        yield Res("sns:topic", arn, tags,
                  lambda arn=arn: sns.tag_resource(ResourceArn=arn, Tags=ctx.tagset()))


def h_kms(ctx):
    kms = ctx.client("kms")
    for k in paginate(kms, "list_keys", "Keys"):
        kid = k["KeyId"]
        md = kms.describe_key(KeyId=kid)["KeyMetadata"]
        # AWS 托管的密钥不能打标签；等待删除、等待导入的也跳过
        if md.get("KeyManager") != "CUSTOMER" or str(md.get("KeyState", "")).startswith("Pending"):
            continue
        tags = {t["TagKey"]: t.get("TagValue", "") for t in paginate(kms, "list_resource_tags", "Tags", KeyId=kid)}
        yield Res("kms:key", md["Arn"], tags,
                  lambda kid=kid: kms.tag_resource(KeyId=kid, Tags=[{"TagKey": ctx.key, "TagValue": ctx.value}]))


def h_dms(ctx):
    dms = ctx.client("dms")
    for r in paginate(dms, "describe_replication_instances", "ReplicationInstances"):
        arn = r["ReplicationInstanceArn"]
        yield Res("dms:replication-instance", arn, tdict(dms.list_tags_for_resource(ResourceArn=arn).get("TagList")),
                  lambda arn=arn: dms.add_tags_to_resource(ResourceArn=arn, Tags=ctx.tagset()))


def h_dms_serverless(ctx):
    dms = ctx.client("dms")
    kwargs = {}
    while True:   # 这个 API 没有分页器，按 Marker 自己翻页
        try:
            resp = dms.describe_replication_configs(**kwargs)
        except ClientError as e:
            # DMS Serverless 没在部分较新的 region 上线，那里调用会返回 AccessDenied。
            # 报错写着 not authorized 的是真的缺 IAM 权限，照常报错
            err = e.response["Error"]
            if err["Code"] == "AccessDeniedException" and "not authorized" not in err.get("Message", ""):
                return
            raise
        for c in resp.get("ReplicationConfigs", []) or []:
            arn = c["ReplicationConfigArn"]
            yield Res("dms:replication-config", arn, tdict(dms.list_tags_for_resource(ResourceArn=arn).get("TagList")),
                      lambda arn=arn: dms.add_tags_to_resource(ResourceArn=arn, Tags=ctx.tagset()))
        if not resp.get("Marker"):
            return
        kwargs["Marker"] = resp["Marker"]


def h_kafka(ctx):
    kafka = ctx.client("kafka")
    for c in paginate(kafka, "list_clusters_v2", "ClusterInfoList"):
        arn = c["ClusterArn"]
        tags = kafka.list_tags_for_resource(ResourceArn=arn).get("Tags", {}) or {}
        yield Res("kafka:cluster", arn, tags,
                  lambda arn=arn: kafka.tag_resource(ResourceArn=arn, Tags=ctx.tagdict()))


def h_route53_resolver(ctx):
    rr = ctx.client("route53resolver")
    items = [("route53resolver:resolver-endpoint", e["Arn"])
             for e in paginate(rr, "list_resolver_endpoints", "ResolverEndpoints")]
    # 跳过 AWS 自带的规则和别的账号共享过来的规则
    items += [("route53resolver:resolver-rule", r["Arn"])
              for r in paginate(rr, "list_resolver_rules", "ResolverRules")
              if str(r.get("OwnerId", "")).isdigit() and r.get("ShareStatus") != "SHARED_WITH_ME"]
    for rtype, arn in items:
        tags = tdict(paginate(rr, "list_tags_for_resource", "Tags", ResourceArn=arn))
        yield Res(rtype, arn, tags, lambda arn=arn: rr.tag_resource(ResourceArn=arn, Tags=ctx.tagset()))


def h_workspaces(ctx):
    ws = ctx.client("workspaces")
    for w in paginate(ws, "describe_workspaces", "Workspaces"):
        if w.get("State") in ("TERMINATING", "TERMINATED"):
            continue
        wid = w["WorkspaceId"]
        yield Res("workspaces:workspace", wid, tdict(ws.describe_tags(ResourceId=wid).get("TagList")),
                  lambda wid=wid: ws.create_tags(ResourceId=wid, Tags=ctx.tagset()))


def h_memorydb(ctx):
    mdb = ctx.client("memorydb")
    for c in paginate(mdb, "describe_clusters", "Clusters"):
        arn = c["ARN"]
        yield Res("memorydb:cluster", arn, tdict(mdb.list_tags(ResourceArn=arn).get("TagList")),
                  lambda arn=arn: mdb.tag_resource(ResourceArn=arn, Tags=ctx.tagset()))


def h_mq(ctx):
    mq = ctx.client("mq")
    for b in paginate(mq, "list_brokers", "BrokerSummaries"):
        arn = b["BrokerArn"]
        yield Res("mq:broker", arn, mq.list_tags(ResourceArn=arn).get("Tags", {}) or {},
                  lambda arn=arn: mq.create_tags(ResourceArn=arn, Tags=ctx.tagdict()))


def h_docdb_elastic(ctx):
    de = ctx.client("docdb-elastic")
    for c in paginate(de, "list_clusters", "clusters"):
        arn = c["clusterArn"]
        yield Res("docdb-elastic:cluster", arn, de.list_tags_for_resource(resourceArn=arn).get("tags", {}) or {},
                  lambda arn=arn: de.tag_resource(resourceArn=arn, tags=ctx.tagdict()))


def h_ds(ctx):
    ds = ctx.client("ds")
    for d in paginate(ds, "describe_directories", "DirectoryDescriptions"):
        if "OwnerDirectoryDescription" in d:
            continue   # 别的账号共享过来的目录
        did = d["DirectoryId"]
        yield Res("ds:directory", did, tdict(paginate(ds, "list_tags_for_resource", "Tags", ResourceId=did)),
                  lambda did=did: ds.add_tags_to_resource(ResourceId=did, Tags=ctx.tagset()))


def h_opensearch(ctx):
    os_ = ctx.client("opensearch")
    names = [d["DomainName"] for d in os_.list_domain_names().get("DomainNames", [])]
    for i in range(0, len(names), 5):   # DescribeDomains 一次最多 5 个
        for s in os_.describe_domains(DomainNames=names[i:i + 5])["DomainStatusList"]:
            if s.get("Deleted"):
                continue
            arn = s["ARN"]
            yield Res("es:domain", arn, tdict(os_.list_tags(ARN=arn).get("TagList")),
                      lambda arn=arn: os_.add_tags(ARN=arn, TagList=ctx.tagset()))


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


def h_route53(ctx):
    r53 = ctx.client("route53", "us-east-1")
    items = [("hostedzone", z["Id"].split("/")[-1]) for z in paginate(r53, "list_hosted_zones", "HostedZones")]
    items += [("healthcheck", h["Id"]) for h in paginate(r53, "list_health_checks", "HealthChecks")]
    for rtype, rid in items:
        tags = tdict(r53.list_tags_for_resource(ResourceType=rtype, ResourceId=rid)["ResourceTagSet"].get("Tags"))
        yield Res(f"route53:{rtype}", f"arn:aws:route53:::{rtype}/{rid}", tags,
                  lambda rtype=rtype, rid=rid: r53.change_tags_for_resource(
                      ResourceType=rtype, ResourceId=rid, AddTags=ctx.tagset()))


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

# 补扫跳过的服务：
#   s3、cloudfront、route53   由 global 那次调用处理
#   cloudformation            栈本身不产生费用；经 Tagging API 给栈打标签会走一次栈更新，把标签传到栈里所有资源，
#                             StackSets 管理的栈还会和 StackSet 对不上
#   budgets、payments         预算和付款方式本身不产生费用，Tagging API 也打不上
SWEEP_SKIP_SERVICES = {"s3", "cloudfront", "route53", "cloudformation", "budgets", "payments"}


def h_sweep(ctx):
    rgt = ctx.client("resourcegroupstaggingapi")
    for m in paginate(rgt, "get_resources", "ResourceTagMappingList"):
        arn = m["ResourceARN"]
        svc = arn.split(":")[2]
        if svc in SWEEP_SKIP_SERVICES:
            continue
        yield Res(f"{svc} (sweep)", arn, tdict(m.get("Tags")),
                  lambda arn=arn: rgt_tag(rgt, [arn], ctx.tagdict()), from_sweep=True)


GLOBAL_HANDLERS = [h_s3, h_cloudfront, h_route53]
REGIONAL_HANDLERS = [
    h_ec2_instances, h_ec2_volumes, h_ec2_snapshots, h_ec2_images,
    h_ec2_eips, h_ec2_natgw, h_ec2_vpce,
    h_rds_instances, h_rds_clusters, h_rds_snapshots, h_rds_cluster_snapshots,
    h_lambda, h_elbv2, h_elb_classic, h_dynamodb, h_efs, h_elasticache, h_eks,
    h_sqs, h_sns, h_kms, h_dms, h_dms_serverless, h_kafka, h_route53_resolver,
    h_workspaces, h_memorydb, h_mq, h_docdb_elastic, h_ds, h_opensearch,
]


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------

def load_mapping():
    """读 SSM 里的账号映射表：{"*": 默认值, "账号ID": 值}。"""
    value = boto3.client("ssm", region_name=REGION, config=BOTO_CFG).get_parameter(
        Name=MAPPING_PARAM)["Parameter"]["Value"]
    mapping = json.loads(value)
    if not isinstance(mapping, dict):
        raise ValueError(f'{MAPPING_PARAM} 应该是 {{"*": "MPE 值", "账号ID": "MPE 值"}} 格式的 JSON 对象')
    return {str(k).strip(): str(v or "").strip() for k, v in mapping.items()}


def value_for(mapping, acct, payer_id):
    """账号该打的标签值：先看账号自己的条目，再看 "*"（不用于 Payer）；被排除时返回 None。"""
    if acct in mapping:
        return mapping[acct] or None
    if acct == payer_id:
        return None
    return mapping.get("*") or None


def list_member_accounts(payer_id):
    """Organizations 里所有 ACTIVE 的成员账号，不含 Payer 自己。"""
    org = boto3.client("organizations", region_name=REGION, config=BOTO_CFG)
    out = []
    for a in paginate(org, "list_accounts", "Accounts"):
        state = a.get("State") or a.get("Status")   # 新字段 State，旧字段 Status
        if state == "ACTIVE" and a["Id"] != payer_id:
            out.append(a["Id"])
    return out


def enabled_regions(sess):
    ec2 = sess.client("ec2", region_name=REGION, config=BOTO_CFG)
    # 不带 AllRegions 参数时，只返回这个账号已启用的 region
    return sorted(r["RegionName"] for r in ec2.describe_regions()["Regions"])


def dispatch(function_name, payer_id):
    """调度：按映射表算出要处理的账号，按“账号 × region”异步调用本函数，每个账号另加一次 global。"""
    mapping = load_mapping()
    for key in [k for k in mapping if k != "*" and not (k.isdigit() and len(k) == 12)]:
        log(account_id=key, region="-", type="mapping", resource="", current_value="",
            action="ERROR", detail='映射表的 key 要么是 "*"，要么是 12 位账号 ID')
        del mapping[key]

    accounts = {k for k in mapping if k != "*"}
    if mapping.get("*"):
        accounts |= set(list_member_accounts(payer_id))
    targets = {}
    for acct in sorted(accounts):
        value = value_for(mapping, acct, payer_id)
        if value:
            targets[acct] = value

    lam = boto3.client("lambda", region_name=REGION, config=BOTO_CFG)
    jobs = 0
    for acct, tag_value in targets.items():
        base = {"account_id": acct, "region": "-", "resource": "", "current_value": ""}
        try:
            regions = REGIONS or enabled_regions(get_session(acct))
        except Exception as e:  # noqa: BLE001
            # 多半是该账号没有 MapTaggerRole，或角色信任的不是本函数的执行角色
            log(**base, type="assume_role", action="ERROR", detail=str(e)[:500])
            continue
        for region in ["global"] + regions:
            try:
                lam.invoke(FunctionName=function_name, InvocationType="Event",
                           Payload=json.dumps({"account": acct, "region": region, "tag_value": tag_value}))
                jobs += 1
            except Exception as e:  # noqa: BLE001
                log(**dict(base, region=region), type="invoke", action="ERROR", detail=str(e)[:500])
    log(action="DISPATCHED", mode="apply" if APPLY else "dryrun",
        detail=f"{len(targets)} 个账号，{jobs} 次扫描调用")
    return {"accounts": len(targets), "jobs": jobs}


def scan(acct, region, tag_value):
    """扫描：处理一个账号的一个 region（或 global），返回各 action 的数量。"""
    counts = {}

    def row(rtype, resource, current_value, action, detail=""):
        log(account_id=acct, region=region, type=rtype, resource=resource,
            current_value=current_value or "", action=action, detail=detail)
        counts[action] = counts.get(action, 0) + 1

    try:
        sess = get_session(acct)
    except Exception as e:  # noqa: BLE001
        row("assume_role", "", "", "ERROR", str(e)[:500])
        return counts

    if region == "global":
        ctx, handlers = Ctx(sess, "us-east-1", TAG_KEY, tag_value), GLOBAL_HANDLERS
    else:
        ctx = Ctx(sess, region, TAG_KEY, tag_value)
        handlers = REGIONAL_HANDLERS + ([] if NO_SWEEP else [h_sweep])

    seen = set()   # 去重：第一层处理过的资源，第二层补扫时跳过
    for h in handlers:
        try:
            for res in h(ctx):
                if res.rid in seen or (res.from_sweep and arn_tail(res.rid) in seen):
                    continue
                seen.add(res.rid)
                cur = res.tags.get(TAG_KEY)
                if cur == tag_value:
                    row(res.rtype, res.rid, cur, "OK")
                elif cur is not None and not OVERWRITE:
                    row(res.rtype, res.rid, cur, "CONFLICT", "已有同名 key 但值不同；OVERWRITE=true 才覆盖")
                elif not APPLY:
                    row(res.rtype, res.rid, cur, "WOULD_TAG")
                else:
                    try:
                        res.apply()
                        row(res.rtype, res.rid, cur, "TAGGED")
                    except Exception as e:  # noqa: BLE001
                        row(res.rtype, res.rid, cur, "FAILED", str(e)[:500])
        except ServiceUnavailable:
            continue   # 服务没在这个 region 上线，不算错误
        except Exception as e:  # noqa: BLE001
            # 某个服务在该 region 不可用 / 没权限 / 被 SCP 拒绝，记下来继续跑
            row(h.__name__, "", "", "ERROR", str(e)[:500])
    return counts


def lambda_handler(event, context):
    event = event if isinstance(event, dict) else {}
    payer_id = context.invoked_function_arn.split(":")[4]   # 本函数所在的账号就是 Payer
    acct = event.get("account")
    if not acct:
        return dispatch(context.function_name, payer_id)

    region = event.get("region")
    if not region:
        raise ValueError('扫描调用要带 region，例如 {"account": "111122223333", "region": "ap-southeast-1"}')
    tag_value = event.get("tag_value") or value_for(load_mapping(), acct, payer_id)
    if not tag_value:
        raise ValueError(f"账号 {acct} 在映射表 {MAPPING_PARAM} 里被排除，或者没有 \"*\" 也没有它的条目")
    counts = scan(acct, region, tag_value)
    return {"account": acct, "region": region, "mode": "apply" if APPLY else "dryrun", "counts": counts}
