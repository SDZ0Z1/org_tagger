#!/usr/bin/env python3
"""
suspend_account.py

暂停或恢复一个成员账号里的资源：停下来，但不删除。配合 suspend-account.json 这条 SCP 使用：
SCP 只能挡住 API 调用，挡不住已经在运行的资源，也管不到 Auto Scaling 这类服务代为执行的操作，
所以要用这个脚本把资源停掉。

在 Payer 的 CloudShell 里运行，经 OrganizationAccountAccessRole 进入目标账号：
  python3 suspend_account.py pause  111122223333            # 只列出会暂停什么，不做改动
  python3 suspend_account.py pause  111122223333 --apply    # 真正暂停
  python3 suspend_account.py resume 111122223333 --apply    # 恢复
选项：
  --regions ap-southeast-1,us-east-1   只处理这些 region；默认是该账号已启用的全部 region
  --role OrganizationAccountAccessRole 进入目标账号用的角色

暂停时做的事（按顺序）：
  Auto Scaling 组    暂停全部流程，避免它把停掉的实例换掉或补新实例
  ECS 服务           期望任务数改成 0
  EC2 实例           停止（EMR 集群的节点除外，停掉会让集群出错）
  RDS、Aurora、DocumentDB、Neptune   停止。注意 AWS 会在停止 7 天后自动启动，暂停期间要每周再跑一次 pause
  Redshift 集群      暂停
  SageMaker 笔记本   停止
  Lambda 函数        预留并发设成 0，所有调用都会被限流（org-tagger 自己的函数除外）
另外列出停不了、会继续计费的资源（CANNOT_PAUSE）：NAT 网关、负载均衡器、ElastiCache、OpenSearch、
MSK、EMR、SageMaker 终端节点、EKS 控制平面、只读副本等。它们只能删除，脚本不删。

暂停前的状态记在资源的 org-suspend 标签里（例如 ECS 原来的任务数、Lambda 原来的预留并发），
resume 只恢复带这个标签的资源，并去掉标签。pause 和 resume 都可以重复运行。
停掉的资源仍按存储收费：EBS 卷、快照、RDS 存储、S3、弹性 IP 等。
"""
import argparse
import sys

import boto3
from botocore.config import Config

KEY = "org-suspend"
SKIP_FUNCTIONS = {"map-realtime-tagger"}
EMR_TAG = "aws:elasticmapreduce:job-flow-id"
ALL_PROCESSES = ["Launch", "Terminate", "AddToLoadBalancer", "AlarmNotification", "AZRebalance",
                 "HealthCheck", "InstanceRefresh", "ReplaceUnhealthy", "ScheduledActions"]
CFG = Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 3, "mode": "standard"})


class Run:
    """一次运行：目标账号、模式和计数。"""

    def __init__(self, sess, account, apply, verb):
        self.sess, self.account, self.apply = sess, account, apply
        self.verb = verb            # PAUSE 或 RESUME
        self.counts = {}

    def client(self, service, region):
        return self.sess.client(service, region_name=region, config=CFG)

    def row(self, region, rtype, rid, action, detail=""):
        self.counts[action] = self.counts.get(action, 0) + 1
        print(f"{region:<15} {rtype:<24} {rid:<48} {action:<13} {detail}".rstrip(), flush=True)

    def act(self, region, rtype, rid, fn, detail=""):
        """dry-run 只记 WOULD_xxx；apply 时执行 fn，成功记 PAUSED/RESUMED，失败记 FAILED。"""
        if not self.apply:
            self.row(region, rtype, rid, f"WOULD_{self.verb}", detail)
            return
        try:
            fn()
            self.row(region, rtype, rid, f"{self.verb}D", detail)
        except Exception as e:  # noqa: BLE001
            self.row(region, rtype, rid, "FAILED", str(e)[:300])


def tdict(tags, k="Key", v="Value"):
    return {t[k]: t.get(v, "") for t in (tags or [])}


def pages(client, method, key, **kwargs):
    if client.can_paginate(method):
        for page in client.get_paginator(method).paginate(**kwargs):
            yield from page.get(key, []) or []
    else:
        yield from getattr(client, method)(**kwargs).get(key, []) or []


# ----------------------------------------------------------------------------
# 每类资源一对函数：pause_xxx / resume_xxx，参数是 (run, region)
# ----------------------------------------------------------------------------

def pause_asg(run, region):
    asg = run.client("autoscaling", region)
    for g in pages(asg, "describe_auto_scaling_groups", "AutoScalingGroups"):
        name = g["AutoScalingGroupName"]
        if KEY in tdict(g.get("Tags")):
            run.row(region, "autoscaling:group", name, "ALREADY")
            continue
        before = ",".join(sorted(p["ProcessName"] for p in g.get("SuspendedProcesses", [])))

        def do(name=name, before=before):
            asg.create_or_update_tags(Tags=[{"ResourceId": name, "ResourceType": "auto-scaling-group",
                                             "Key": KEY, "Value": before, "PropagateAtLaunch": False}])
            asg.suspend_processes(AutoScalingGroupName=name)
        run.act(region, "autoscaling:group", name, do, f"原来已暂停的流程：{before or '无'}")


def resume_asg(run, region):
    asg = run.client("autoscaling", region)
    for g in pages(asg, "describe_auto_scaling_groups", "AutoScalingGroups"):
        name, tags = g["AutoScalingGroupName"], tdict(g.get("Tags"))
        if KEY not in tags:
            continue
        before = {p for p in tags[KEY].split(",") if p}
        to_resume = [p["ProcessName"] for p in g.get("SuspendedProcesses", []) if p["ProcessName"] not in before]

        def do(name=name, to_resume=to_resume):
            if to_resume:
                asg.resume_processes(AutoScalingGroupName=name, ScalingProcesses=to_resume)
            asg.delete_tags(Tags=[{"ResourceId": name, "ResourceType": "auto-scaling-group", "Key": KEY}])
        run.act(region, "autoscaling:group", name, do, f"恢复的流程：{','.join(to_resume) or '无'}")


def ecs_services(ecs):
    for cluster in pages(ecs, "list_clusters", "clusterArns"):
        arns = list(pages(ecs, "list_services", "serviceArns", cluster=cluster))
        for i in range(0, len(arns), 10):
            for s in ecs.describe_services(cluster=cluster, services=arns[i:i + 10], include=["TAGS"])["services"]:
                yield cluster, s


def pause_ecs(run, region):
    ecs = run.client("ecs", region)
    for cluster, s in ecs_services(ecs):
        arn, desired = s["serviceArn"], s.get("desiredCount", 0)
        if KEY in tdict(s.get("tags"), "key", "value"):
            run.row(region, "ecs:service", arn.split("/")[-1], "ALREADY")
            continue
        if s.get("status") != "ACTIVE" or desired == 0:
            continue

        def do(cluster=cluster, arn=arn, desired=desired):
            ecs.tag_resource(resourceArn=arn, tags=[{"key": KEY, "value": str(desired)}])
            ecs.update_service(cluster=cluster, service=arn, desiredCount=0)
        run.act(region, "ecs:service", arn.split("/")[-1], do, f"任务数 {desired} → 0")


def resume_ecs(run, region):
    ecs = run.client("ecs", region)
    for cluster, s in ecs_services(ecs):
        arn, tags = s["serviceArn"], tdict(s.get("tags"), "key", "value")
        if KEY not in tags:
            continue
        desired = int(tags[KEY] or 0)

        def do(cluster=cluster, arn=arn, desired=desired):
            ecs.update_service(cluster=cluster, service=arn, desiredCount=desired)
            ecs.untag_resource(resourceArn=arn, tagKeys=[KEY])
        run.act(region, "ecs:service", arn.split("/")[-1], do, f"任务数 → {desired}")


def pause_ec2(run, region):
    ec2 = run.client("ec2", region)
    for r in pages(ec2, "describe_instances", "Reservations",
                   Filters=[{"Name": "instance-state-name", "Values": ["running"]}]):
        for i in r["Instances"]:
            iid, tags = i["InstanceId"], tdict(i.get("Tags"))
            if EMR_TAG in tags:
                continue   # 在 EMR 那一项里列为 CANNOT_PAUSE

            def do(iid=iid):
                ec2.create_tags(Resources=[iid], Tags=[{"Key": KEY, "Value": "stopped"}])
                ec2.stop_instances(InstanceIds=[iid])
            run.act(region, "ec2:instance", iid, do)


def resume_ec2(run, region):
    ec2 = run.client("ec2", region)
    for r in pages(ec2, "describe_instances", "Reservations", Filters=[{"Name": "tag-key", "Values": [KEY]}]):
        for i in r["Instances"]:
            iid, state = i["InstanceId"], i["State"]["Name"]
            if state in ("terminated", "shutting-down"):
                continue

            def do(iid=iid, state=state):
                if state == "stopped":
                    ec2.start_instances(InstanceIds=[iid])
                ec2.delete_tags(Resources=[iid], Tags=[{"Key": KEY}])
            run.act(region, "ec2:instance", iid, do, "" if state == "stopped" else f"现在是 {state}，只去掉标签")


def pause_rds(run, region):
    rds = run.client("rds", region)
    for c in pages(rds, "describe_db_clusters", "DBClusters"):
        cid = c["DBClusterIdentifier"]
        if c.get("Status") != "available":
            continue

        def do(c=c, cid=cid):
            rds.add_tags_to_resource(ResourceName=c["DBClusterArn"], Tags=[{"Key": KEY, "Value": "stopped"}])
            rds.stop_db_cluster(DBClusterIdentifier=cid)
        run.act(region, "rds:cluster", cid, do, f"{c.get('Engine')}；7 天后 AWS 会自动启动，暂停期间每周再跑一次 pause")
    for d in pages(rds, "describe_db_instances", "DBInstances"):
        did = d["DBInstanceIdentifier"]
        if d.get("DBClusterIdentifier") or d.get("DBInstanceStatus") != "available":
            continue   # 集群成员随集群一起停
        if d.get("ReadReplicaSourceDBInstanceIdentifier") or d.get("ReadReplicaDBInstanceIdentifiers"):
            run.row(region, "rds:db", did, "CANNOT_PAUSE", "只读副本和有只读副本的实例不能停止")
            continue

        def do(d=d, did=did):
            rds.add_tags_to_resource(ResourceName=d["DBInstanceArn"], Tags=[{"Key": KEY, "Value": "stopped"}])
            rds.stop_db_instance(DBInstanceIdentifier=did)
        run.act(region, "rds:db", did, do, "7 天后 AWS 会自动启动，暂停期间每周再跑一次 pause")


def resume_rds(run, region):
    rds = run.client("rds", region)
    for c in pages(rds, "describe_db_clusters", "DBClusters"):
        if KEY not in tdict(c.get("TagList")):
            continue
        cid, status = c["DBClusterIdentifier"], c.get("Status")

        def do(c=c, cid=cid, status=status):
            if status == "stopped":
                rds.start_db_cluster(DBClusterIdentifier=cid)
            rds.remove_tags_from_resource(ResourceName=c["DBClusterArn"], TagKeys=[KEY])
        run.act(region, "rds:cluster", cid, do, "" if status == "stopped" else f"现在是 {status}，只去掉标签")
    for d in pages(rds, "describe_db_instances", "DBInstances"):
        if d.get("DBClusterIdentifier") or KEY not in tdict(d.get("TagList")):
            continue
        did, status = d["DBInstanceIdentifier"], d.get("DBInstanceStatus")

        def do(d=d, did=did, status=status):
            if status == "stopped":
                rds.start_db_instance(DBInstanceIdentifier=did)
            rds.remove_tags_from_resource(ResourceName=d["DBInstanceArn"], TagKeys=[KEY])
        run.act(region, "rds:db", did, do, "" if status == "stopped" else f"现在是 {status}，只去掉标签")


def redshift_arn(run, region, cid):
    return f"arn:aws:redshift:{region}:{run.account}:cluster:{cid}"


def pause_redshift(run, region):
    rs = run.client("redshift", region)
    for c in pages(rs, "describe_clusters", "Clusters"):
        cid = c["ClusterIdentifier"]
        if c.get("ClusterStatus") != "available":
            continue

        def do(cid=cid):
            rs.create_tags(ResourceName=redshift_arn(run, region, cid), Tags=[{"Key": KEY, "Value": "paused"}])
            rs.pause_cluster(ClusterIdentifier=cid)
        run.act(region, "redshift:cluster", cid, do)


def resume_redshift(run, region):
    rs = run.client("redshift", region)
    for c in pages(rs, "describe_clusters", "Clusters"):
        cid = c["ClusterIdentifier"]
        if KEY not in tdict(c.get("Tags")):
            continue
        status = c.get("ClusterStatus")

        def do(cid=cid, status=status):
            if status == "paused":
                rs.resume_cluster(ClusterIdentifier=cid)
            rs.delete_tags(ResourceName=redshift_arn(run, region, cid), TagKeys=[KEY])
        run.act(region, "redshift:cluster", cid, do, "" if status == "paused" else f"现在是 {status}，只去掉标签")


def pause_sagemaker(run, region):
    sm = run.client("sagemaker", region)
    for n in pages(sm, "list_notebook_instances", "NotebookInstances"):
        if n.get("NotebookInstanceStatus") != "InService":
            continue
        name, arn = n["NotebookInstanceName"], n["NotebookInstanceArn"]

        def do(name=name, arn=arn):
            sm.add_tags(ResourceArn=arn, Tags=[{"Key": KEY, "Value": "stopped"}])
            sm.stop_notebook_instance(NotebookInstanceName=name)
        run.act(region, "sagemaker:notebook", name, do)
    for e in pages(sm, "list_endpoints", "Endpoints"):
        if e.get("EndpointStatus") == "InService":
            run.row(region, "sagemaker:endpoint", e["EndpointName"], "CANNOT_PAUSE", "终端节点只能删除")


def resume_sagemaker(run, region):
    sm = run.client("sagemaker", region)
    for n in pages(sm, "list_notebook_instances", "NotebookInstances"):
        name, arn, status = n["NotebookInstanceName"], n["NotebookInstanceArn"], n.get("NotebookInstanceStatus")
        if KEY not in tdict(sm.list_tags(ResourceArn=arn).get("Tags")):
            continue

        def do(name=name, arn=arn, status=status):
            if status == "Stopped":
                sm.start_notebook_instance(NotebookInstanceName=name)
            sm.delete_tags(ResourceArn=arn, TagKeys=[KEY])
        run.act(region, "sagemaker:notebook", name, do, "" if status == "Stopped" else f"现在是 {status}，只去掉标签")


def pause_lambda(run, region):
    lam = run.client("lambda", region)
    for f in pages(lam, "list_functions", "Functions"):
        name, arn = f["FunctionName"], f["FunctionArn"]
        if name in SKIP_FUNCTIONS:
            continue
        if KEY in (lam.list_tags(Resource=arn).get("Tags") or {}):
            run.row(region, "lambda:function", name, "ALREADY")
            continue
        before = lam.get_function_concurrency(FunctionName=name).get("ReservedConcurrentExecutions")
        prev = "none" if before is None else str(before)

        def do(name=name, arn=arn, prev=prev):
            lam.tag_resource(Resource=arn, Tags={KEY: prev})
            lam.put_function_concurrency(FunctionName=name, ReservedConcurrentExecutions=0)
        run.act(region, "lambda:function", name, do, f"预留并发 {prev} → 0")


def resume_lambda(run, region):
    lam = run.client("lambda", region)
    for f in pages(lam, "list_functions", "Functions"):
        name, arn = f["FunctionName"], f["FunctionArn"]
        tags = lam.list_tags(Resource=arn).get("Tags") or {}
        if KEY not in tags:
            continue
        prev = tags[KEY]

        def do(name=name, arn=arn, prev=prev):
            if prev == "none":
                lam.delete_function_concurrency(FunctionName=name)
            else:
                lam.put_function_concurrency(FunctionName=name, ReservedConcurrentExecutions=int(prev))
            lam.untag_resource(Resource=arn, TagKeys=[KEY])
        run.act(region, "lambda:function", name, do, f"预留并发 → {prev}")


def report_unpausable(run, region):
    """停不了的资源只列出来，它们会继续计费。"""
    ec2 = run.client("ec2", region)
    for n in pages(ec2, "describe_nat_gateways", "NatGateways",
                   Filters=[{"Name": "state", "Values": ["available"]}]):
        run.row(region, "ec2:natgateway", n["NatGatewayId"], "CANNOT_PAUSE", "只能删除")
    for lb in pages(run.client("elbv2", region), "describe_load_balancers", "LoadBalancers"):
        run.row(region, "elbv2:loadbalancer", lb["LoadBalancerName"], "CANNOT_PAUSE", "只能删除")
    for lb in pages(run.client("elb", region), "describe_load_balancers", "LoadBalancerDescriptions"):
        run.row(region, "elb:loadbalancer", lb["LoadBalancerName"], "CANNOT_PAUSE", "只能删除")
    for c in pages(run.client("elasticache", region), "describe_cache_clusters", "CacheClusters"):
        run.row(region, "elasticache:cluster", c["CacheClusterId"], "CANNOT_PAUSE", "只能删除")
    for d in run.client("opensearch", region).list_domain_names().get("DomainNames", []):
        run.row(region, "opensearch:domain", d["DomainName"], "CANNOT_PAUSE", "只能删除")
    for c in pages(run.client("kafka", region), "list_clusters_v2", "ClusterInfoList"):
        run.row(region, "kafka:cluster", c["ClusterName"], "CANNOT_PAUSE", "只能删除")
    for c in pages(run.client("emr", region), "list_clusters", "Clusters",
                   ClusterStates=["STARTING", "BOOTSTRAPPING", "RUNNING", "WAITING"]):
        run.row(region, "emr:cluster", c["Id"], "CANNOT_PAUSE", "停掉节点会让集群出错，只能终止")
    for name in pages(run.client("eks", region), "list_clusters", "clusters"):
        run.row(region, "eks:cluster", name, "CANNOT_PAUSE", "控制平面按小时收费；节点随 Auto Scaling 组停止")


PAUSE = [pause_asg, pause_ecs, pause_ec2, pause_rds, pause_redshift, pause_sagemaker, pause_lambda,
         report_unpausable]
RESUME = [resume_rds, resume_redshift, resume_ec2, resume_asg, resume_ecs, resume_sagemaker, resume_lambda]


def session_for(account, role):
    c = boto3.client("sts").assume_role(RoleArn=f"arn:aws:iam::{account}:role/{role}",
                                        RoleSessionName="suspend-account")["Credentials"]
    return boto3.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
                         aws_session_token=c["SessionToken"])


def main(argv=None):
    p = argparse.ArgumentParser(description="暂停或恢复一个成员账号里的资源（不删除）")
    p.add_argument("command", choices=["pause", "resume"])
    p.add_argument("account")
    p.add_argument("--apply", action="store_true", help="真正执行；不加只列出会做什么")
    p.add_argument("--regions", default="", help="逗号分隔；默认是该账号已启用的全部 region")
    p.add_argument("--role", default="OrganizationAccountAccessRole")
    args = p.parse_args(argv)

    try:
        sess = session_for(args.account, args.role)
    except Exception as e:  # noqa: BLE001
        print(f"进不了账号 {args.account}：{e}\n"
              f"确认这个账号里有 {args.role}，并且信任 Payer。受邀加入组织的账号默认没有这个角色。")
        return 1
    run = Run(sess, args.account, args.apply, "PAUSE" if args.command == "pause" else "RESUME")
    regions = [r.strip() for r in args.regions.split(",") if r.strip()] or \
        sorted(r["RegionName"] for r in run.client("ec2", "us-east-1").describe_regions()["Regions"])

    print(f"账号 {args.account}，{args.command}，{'执行' if args.apply else '只列出（加 --apply 才执行）'}，"
          f"{len(regions)} 个 region")
    for region in regions:
        for h in (PAUSE if args.command == "pause" else RESUME):
            try:
                h(run, region)
            except Exception as e:  # noqa: BLE001
                run.row(region, h.__name__, "", "ERROR", str(e)[:300])
    print("汇总：" + "，".join(f"{k} {v}" for k, v in sorted(run.counts.items())) if run.counts else "汇总：没有资源")
    return 1 if run.counts.get("FAILED") else 0


if __name__ == "__main__":
    sys.exit(main())
