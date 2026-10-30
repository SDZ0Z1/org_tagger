#!/usr/bin/env python3
"""
make_templates.py

生成 templates/ 下给 CloudFormation StackSets 用的两个模板：
  map-tagger-roles.yaml     MapTaggerRole（供 Payer 的每日扫描 assume）和 MapRealtimeTaggerRole（实时层的执行角色）。
                            IAM 是全局的，每个账号只部署到一个 region。
  map-tagger-realtime.yaml  实时层 Lambda 和 EventBridge 规则，每个账号部署到每个用到的 region。

两个 StackSet 都部署到整个组织并打开自动部署，每个成员账号都会有这些资源；
在账号映射表里被排除的账号，两层都不处理，这些资源在那里只是空转。

实时层的代码和 EventBridge 事件模式都直接从 realtime_tagger.py 取。
改了 realtime_tagger.py 或下面的权限列表后，重新运行：
  python make_templates.py
再用新生成的模板更新对应的 StackSet。
"""
import json
import re
from pathlib import Path

import realtime_tagger

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "templates"
SWEEP_ROLE = "MapSweepLambdaRole"      # Payer 里扫描 Lambda 的执行角色，MapTaggerRole 只信任它
INLINE_POLICY_LIMIT = 10240            # 一个角色的内联策略加起来的字符上限（不算空白）
EVENT_PATTERN_LIMIT = 4096             # EventBridge 规则事件模式的字符上限


# ----------------------------------------------------------------------------
# 权限列表
# ----------------------------------------------------------------------------

# MapTaggerRole：每日扫描第一层枚举各类型资源、读标签，和 sweep_tagger.py 的 h_xxx 对应
SWEEP_READ_ACTIONS = [
    "tag:GetResources",
    "ec2:DescribeRegions", "ec2:DescribeInstances", "ec2:DescribeVolumes",
    "ec2:DescribeSnapshots", "ec2:DescribeImages", "ec2:DescribeAddresses",
    "ec2:DescribeNatGateways", "ec2:DescribeVpcEndpoints",
    "rds:DescribeDBInstances", "rds:DescribeDBClusters",
    "rds:DescribeDBSnapshots", "rds:DescribeDBClusterSnapshots",
    "lambda:ListFunctions", "lambda:ListTags",
    "elasticloadbalancing:DescribeLoadBalancers", "elasticloadbalancing:DescribeTags",
    "dynamodb:ListTables", "dynamodb:DescribeTable", "dynamodb:ListTagsOfResource",
    "elasticfilesystem:DescribeFileSystems",
    "elasticache:DescribeCacheClusters", "elasticache:ListTagsForResource",
    "eks:ListClusters", "eks:DescribeCluster",
    "s3:ListAllMyBuckets", "s3:GetBucketLocation", "s3:GetBucketTagging",
    "cloudfront:ListDistributions", "cloudfront:ListTagsForResource",
    "sqs:ListQueues", "sqs:ListQueueTags", "sns:ListTopics", "sns:ListTagsForResource",
    "kms:ListKeys", "kms:DescribeKey", "kms:ListResourceTags",
    "dms:DescribeReplicationInstances", "dms:DescribeReplicationConfigs", "dms:ListTagsForResource",
    "kafka:ListClustersV2", "kafka:ListTagsForResource",
    "route53:ListHostedZones", "route53:ListHealthChecks", "route53:ListTagsForResource",
    "route53resolver:ListResolverEndpoints", "route53resolver:ListResolverRules",
    "route53resolver:ListTagsForResource",
    "workspaces:DescribeWorkspaces", "workspaces:DescribeTags",
    "memorydb:DescribeClusters", "memorydb:ListTags", "mq:ListBrokers", "mq:ListTags",
    "docdb-elastic:ListClusters", "docdb-elastic:ListTagsForResource",
    "ds:DescribeDirectories", "ds:ListTagsForResource",
    "es:ListDomainNames", "es:DescribeDomains", "es:ListTags",
]

# MapTaggerRole：第一层各类型的打标签权限。
# S3 经 Tagging API 打标签时会把桶已有的标签一起写回去，所以这一段不加 key 限制。
SWEEP_FIRST_LAYER_TAG_ACTIONS = [
    "tag:TagResources", "ec2:CreateTags", "rds:AddTagsToResource",
    "lambda:TagResource", "elasticloadbalancing:AddTags", "dynamodb:TagResource",
    "elasticfilesystem:TagResource", "elasticache:AddTagsToResource",
    "eks:TagResource", "s3:PutBucketTagging", "cloudfront:TagResource",
]

# MapTaggerRole：第二层补扫可能找到的各种类型，尽量覆盖所有 AWS 服务的打标签操作。
# 由 AWS 策略生成器的服务列表（https://awspolicygen.s3.amazonaws.com/js/policies.js）整理：
# 每个服务以 Tag 开头的操作写成“服务:Tag*”，再加上 AddTags*、CreateTags 这类不以 Tag 开头的打标签操作；
# 去掉第一层已有的，以及 iam、sts、organizations、sso 这类和费用无关、又涉及权限的服务。
# 这一段加了条件，只允许写 TagKey 这一个 key。
SWEEP_TAG_ACTIONS = [
    "a4b:Tag*", "account-access:Tag*", "acm:Tag*", "acm:AddTagsToCertificate", "acm-pca:Tag*",
    "aco-automation:Tag*", "agent-registry:Tag*", "aidevops:Tag*", "aiops:Tag*", "airflow:Tag*",
    "airflow-serverless:Tag*", "amplify:Tag*", "amplifyuibuilder:Tag*", "aoss:Tag*",
    "app-integrations:Tag*", "appconfig:Tag*", "appfabric:Tag*", "appflow:Tag*",
    "application-autoscaling:Tag*", "application-signals:Tag*", "applicationinsights:Tag*",
    "appmesh:Tag*", "apprunner:Tag*", "appstream:Tag*", "appsync:Tag*", "apptest:Tag*", "aps:Tag*",
    "arc-region-switch:Tag*", "artifact:Tag*", "assurance:Tag*", "athena:Tag*", "auditmanager:Tag*",
    "aws-external-anthropic:Tag*", "aws-marketplace:Tag*", "b2bi:Tag*", "backup:Tag*",
    "backup-gateway:Tag*", "backup-search:Tag*", "batch:Tag*", "bcm-dashboards:Tag*",
    "bcm-data-exports:Tag*", "bcm-pricing-calculator:Tag*", "bedrock:Tag*",
    "bedrock-agentcore:Tag*", "bedrock-mantle:Tag*", "billing:Tag*", "billingconductor:Tag*",
    "braket:Tag*", "budgets:Tag*", "bugbust:Tag*", "cases:Tag*", "cassandra:Tag*", "ce:Tag*",
    "chatbot:Tag*", "chime:Tag*", "cleanrooms:Tag*", "cleanrooms-ml:Tag*", "cloud9:Tag*",
    "clouddirectory:Tag*", "cloudformation:Tag*", "cloudhsm:Tag*", "cloudsearch:AddTags",
    "cloudtrail:AddTags", "cloudwatch:Tag*", "codeartifact:Tag*", "codecatalyst:Tag*",
    "codecommit:Tag*", "codeconnections:Tag*", "codedeploy:Tag*",
    "codedeploy:AddTagsToOnPremisesInstances", "codeguru-profiler:Tag*", "codeguru-reviewer:Tag*",
    "codeguru-security:Tag*", "codepipeline:Tag*", "codestar-connections:Tag*",
    "codestar-notifications:Tag*", "codewhisperer:Tag*", "cognito-identity:Tag*",
    "cognito-idp:Tag*", "comprehend:Tag*", "config:Tag*", "connect:Tag*", "connect-campaigns:Tag*",
    "controltower:Tag*", "cur:Tag*", "databrew:Tag*", "dataexchange:Tag*", "datapipeline:AddTags",
    "datasync:Tag*", "datazone:Tag*", "dax:Tag*", "deadline:Tag*", "detective:Tag*",
    "devicefarm:Tag*", "directconnect:Tag*", "discovery:CreateTags", "dlm:Tag*",
    "dms:AddTagsToResource", "docdb-elastic:Tag*", "drs:Tag*", "ds:AddTagsToResource", "dsql:Tag*",
    "ecr:Tag*", "ecr-public:Tag*", "ecs:Tag*", "elasticbeanstalk:AddTags",
    "elasticbeanstalk:UpdateTagsForResource", "elasticfilesystem:CreateTags",
    "elasticmapreduce:AddTags", "elemental-inference:Tag*", "elemental-support-cases:Tag*",
    "emr-containers:Tag*", "emr-serverless:Tag*", "end-user-messaging:Tag*",
    "entityresolution:Tag*", "es:AddTags", "events:Tag*", "evs:Tag*", "finspace:Tag*",
    "firehose:Tag*", "fis:Tag*", "fms:Tag*", "forecast:Tag*", "frauddetector:Tag*", "fsx:Tag*",
    "gamelift:Tag*", "gameliftstreams:Tag*", "geo:Tag*", "glacier:AddTagsToVault",
    "globalaccelerator:Tag*", "glue:Tag*", "grafana:Tag*", "greengrass:Tag*", "groundstation:Tag*",
    "guardduty:Tag*", "health-agent:Tag*", "healthlake:Tag*", "honeycode:Tag*", "imagebuilder:Tag*",
    "inspector2:Tag*", "interconnect:Tag*", "internetmonitor:Tag*", "invoicing:Tag*", "iot:Tag*",
    "iotanalytics:Tag*", "iotdeviceadvisor:Tag*", "iotfleethub:Tag*", "iotfleetwise:Tag*",
    "iotmanagedintegrations:Tag*", "iotsitewise:Tag*", "iottwinmaker:Tag*", "iotwireless:Tag*",
    "ivs:Tag*", "ivschat:Tag*", "kafka:Tag*", "kafkaconnect:Tag*", "kendra:Tag*",
    "kendra-ranking:Tag*", "kinesis:Tag*", "kinesis:AddTagsToStream", "kinesisanalytics:Tag*",
    "kinesisvideo:Tag*", "kms:Tag*", "launchwizard:Tag*", "lex:Tag*", "license-manager:Tag*",
    "license-manager-linux-subscriptions:Tag*", "license-manager-user-subscriptions:Tag*",
    "lightsail:Tag*", "logs:Tag*", "lookoutequipment:Tag*", "lookoutmetrics:Tag*",
    "lookoutvision:Tag*", "m2:Tag*", "machinelearning:AddTags", "macie2:Tag*",
    "managedblockchain:Tag*", "mediaconnect:Tag*", "mediaconvert:Tag*", "medialive:CreateTags",
    "mediapackage:Tag*", "mediapackage-vod:Tag*", "mediapackagev2:Tag*", "mediastore:Tag*",
    "mediatailor:Tag*", "medical-imaging:Tag*", "memorydb:Tag*", "mgh:Tag*", "mgn:Tag*",
    "migrationhub-orchestrator:Tag*", "mobiletargeting:Tag*", "monitron:Tag*", "mpa:Tag*",
    "mq:CreateTags", "neptune-graph:Tag*", "network-firewall:Tag*", "network-security-manager:Tag*",
    "networkflowmonitor:Tag*", "networkmanager:Tag*", "networkmonitor:Tag*", "nimble:Tag*",
    "notifications:Tag*", "notifications-contacts:Tag*", "oam:Tag*", "observabilityadmin:Tag*",
    "odb:Tag*", "omics:Tag*", "one:Tag*", "opsworks:Tag*", "opsworks-cm:Tag*", "osis:Tag*",
    "outposts:Tag*", "panorama:Tag*", "partnercentral:Tag*", "payment-cryptography:Tag*",
    "payments:Tag*", "pca-connector-ad:Tag*", "pca-connector-scep:Tag*", "pcs:Tag*",
    "personalize:Tag*", "pi:Tag*", "pipes:Tag*", "private-networks:Tag*", "profile:Tag*",
    "proton:Tag*", "purchase-orders:Tag*", "q:Tag*", "qapps:Tag*", "qbusiness:Tag*",
    "qdeveloper:Tag*", "qldb:Tag*", "quicksight:Tag*", "ram:Tag*", "rbin:Tag*",
    "redshift:CreateTags", "redshift-serverless:Tag*", "refactor-spaces:Tag*", "rekognition:Tag*",
    "repostspace:Tag*", "resiliencehub:Tag*", "resource-explorer-2:Tag*", "resource-groups:Tag*",
    "robomaker:Tag*", "rolesanywhere:Tag*", "route53:ChangeTagsForResource",
    "route53-recovery-control-config:Tag*", "route53-recovery-readiness:Tag*",
    "route53domains:UpdateTagsForDomain", "route53globalresolver:Tag*", "route53profiles:Tag*",
    "route53resolver:Tag*", "rtbfabric:Tag*", "rum:Tag*", "s3:Tag*", "s3express:Tag*",
    "s3files:Tag*", "s3tables:Tag*", "s3vectors:Tag*", "sagemaker:AddTags",
    "sagemaker-geospatial:Tag*", "savingsplans:Tag*", "scheduler:Tag*", "schemas:Tag*", "scn:Tag*",
    "secretsmanager:Tag*", "security-ir:Tag*", "securityagent:Tag*", "securityhub:Tag*",
    "securitylake:Tag*", "servicecatalog:Tag*", "servicediscovery:Tag*", "servicequotas:Tag*",
    "ses:Tag*", "shield:Tag*", "signer:Tag*", "simspaceweaver:Tag*", "sms-voice:Tag*",
    "snow-device-management:Tag*", "sns:Tag*", "social-messaging:Tag*", "sqlworkbench:Tag*",
    "sqs:Tag*", "ssm:AddTagsToResource", "ssm-contacts:Tag*", "ssm-incidents:Tag*",
    "ssm-quicksetup:Tag*", "ssm-sap:Tag*", "states:Tag*", "storagegateway:AddTagsToResource",
    "supportauthz:Tag*", "swf:Tag*", "synthetics:Tag*", "textract:Tag*", "thinclient:Tag*",
    "timestream:Tag*", "timestream-influxdb:Tag*", "tnb:Tag*", "transcribe:Tag*", "transfer:Tag*",
    "transform:Tag*", "transform-custom:Tag*", "translate:Tag*", "ts:Tag*", "vendor-insights:Tag*",
    "verifiedpermissions:Tag*", "voiceid:Tag*", "vpc-lattice:Tag*", "waf:Tag*", "waf-regional:Tag*",
    "wafv2:Tag*", "wellarchitected:Tag*", "wickr:Tag*", "wisdom:Tag*", "worklink:Tag*",
    "workmail:Tag*", "workspaces:CreateTags", "workspaces-instances:Tag*", "workspaces-web:Tag*",
    "xray:Tag*",
]

# MapRealtimeTaggerRole：和 realtime_tagger.py 的 ADAPTERS 对应
REALTIME_READ_ACTIONS = [
    "ec2:DescribeTags", "ec2:DescribeInstances",
    "rds:ListTagsForResource",
    "lambda:ListTags",
    "elasticloadbalancing:DescribeTags",
    "dynamodb:ListTagsOfResource",
    "elasticfilesystem:DescribeFileSystems",
    "elasticache:ListTagsForResource",
    "eks:ListTagsForResource",
    "s3:GetBucketLocation", "s3:GetBucketTagging",
    "cloudfront:ListTagsForResource",
    "sqs:ListQueueTags", "sns:ListTagsForResource", "kms:ListResourceTags", "dms:ListTagsForResource",
    "kafka:ListTagsForResource", "route53:ListTagsForResource", "route53resolver:ListTagsForResource",
    "workspaces:DescribeTags", "memorydb:ListTags", "mq:ListTags", "docdb-elastic:ListTagsForResource",
    "ds:ListTagsForResource", "es:ListTags",
]
REALTIME_TAG_ACTIONS = [
    "ec2:CreateTags", "rds:AddTagsToResource", "lambda:TagResource",
    "elasticloadbalancing:AddTags", "dynamodb:TagResource",
    "elasticfilesystem:TagResource", "elasticache:AddTagsToResource",
    "eks:TagResource", "s3:PutBucketTagging", "tag:TagResources",
    "cloudfront:TagResource",
    "sqs:TagQueue", "sns:TagResource", "kms:TagResource", "dms:AddTagsToResource",
    "kafka:TagResource", "route53:ChangeTagsForResource", "route53resolver:TagResource",
    "workspaces:CreateTags", "memorydb:TagResource", "mq:CreateTags", "docdb-elastic:TagResource",
    "ds:AddTagsToResource", "es:AddTags",
]


# ----------------------------------------------------------------------------
# 模板内容
# ----------------------------------------------------------------------------

class Literal(str):
    """输出成 YAML 的 | 块，用于内联代码。"""


def statement(sid, actions, condition=None):
    s = {"Sid": sid, "Effect": "Allow", "Action": actions, "Resource": "*"}
    if condition:
        s["Condition"] = condition
    return s


def policy(*statements):
    return {"Version": "2012-10-17", "Statement": list(statements)}


MAPPING_PARAM_PARAMETER = {
    "Type": "String",
    "AllowedPattern": "^arn:aws:ssm:[a-z0-9-]+:[0-9]{12}:parameter/.+$",
    "Description": "账号映射表 SSM 参数的完整 ARN，如 arn:aws:ssm:ap-southeast-1:123456789012:parameter/map-tagger/accounts",
}
TAG_KEY_PARAMETER = {"Type": "String", "Default": "map-migrated", "Description": "标签 key"}

MAP_TAGGER_POLICY = policy(
    statement("ListAndReadTags", SWEEP_READ_ACTIONS),
    statement("TagFirstLayerTypes", SWEEP_FIRST_LAYER_TAG_ACTIONS),
    statement("TagOtherTypes", SWEEP_TAG_ACTIONS,
              {"ForAllValues:StringEquals": {"aws:TagKeys": [{"Ref": "TagKey"}]}}),
)

ROLES_TEMPLATE = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Description": "org-tagger: MapTaggerRole 和 MapRealtimeTaggerRole。每个成员账号只部署到一个 region。",
    "Parameters": {
        "PayerAccountId": {"Type": "String", "AllowedPattern": "^[0-9]{12}$",
                           "Description": "Payer（管理账号）的账号 ID"},
        "MappingParameterArn": MAPPING_PARAM_PARAMETER,
        "TagKey": TAG_KEY_PARAMETER,
    },
    "Resources": {
        "MapTaggerRole": {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "RoleName": "MapTaggerRole",
                "AssumeRolePolicyDocument": policy({
                    "Effect": "Allow",
                    "Principal": {"AWS": {"Fn::Sub": "arn:${AWS::Partition}:iam::${PayerAccountId}:role/" + SWEEP_ROLE}},
                    "Action": "sts:AssumeRole",
                }),
                "Policies": [{"PolicyName": "MapTagger", "PolicyDocument": MAP_TAGGER_POLICY}],
            },
        },
        "MapRealtimeTaggerRole": {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "RoleName": "MapRealtimeTaggerRole",
                "AssumeRolePolicyDocument": policy({
                    "Effect": "Allow",
                    "Principal": {"Service": "lambda.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }),
                "ManagedPolicyArns": [
                    {"Fn::Sub": "arn:${AWS::Partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"},
                ],
                "Policies": [{"PolicyName": "MapRealtimeTagger", "PolicyDocument": policy(
                    statement("ReadTags", REALTIME_READ_ACTIONS),
                    statement("AddTags", REALTIME_TAG_ACTIONS),
                    {"Sid": "ReadMapping", "Effect": "Allow", "Action": "ssm:GetParameter",
                     "Resource": {"Ref": "MappingParameterArn"}},
                )}],
            },
        },
    },
}

REALTIME_TEMPLATE = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Description": "org-tagger 实时层: Lambda map-realtime-tagger 和 EventBridge 规则。"
                   "每个成员账号部署到每个用到的 region（含 us-east-1）。",
    "Parameters": {
        "MappingParameterArn": MAPPING_PARAM_PARAMETER,
        "TagKey": TAG_KEY_PARAMETER,
        "Mode": {"Type": "String", "Default": "dryrun", "AllowedValues": ["dryrun", "apply"],
                 "Description": "dryrun 只写日志；apply 才真正打标签"},
    },
    "Resources": {
        "Function": {
            "Type": "AWS::Lambda::Function",
            "Properties": {
                "FunctionName": "map-realtime-tagger",
                "Runtime": "python3.13",
                "Handler": "index.lambda_handler",
                "Role": {"Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/MapRealtimeTaggerRole"},
                "Timeout": 300,
                "MemorySize": 128,
                "Environment": {"Variables": {
                    "MAPPING_PARAM": {"Ref": "MappingParameterArn"},
                    "TAG_KEY": {"Ref": "TagKey"},
                    "MODE": {"Ref": "Mode"},
                }},
                "Code": {"ZipFile": Literal((ROOT / "realtime_tagger.py").read_text(encoding="utf-8"))},
            },
        },
        "Rule": {
            "Type": "AWS::Events::Rule",
            "Properties": {
                "Name": "map-realtime-tagger",
                "EventPattern": realtime_tagger.event_pattern(),
                "State": "ENABLED",
                "Targets": [{"Id": "map-realtime-tagger", "Arn": {"Fn::GetAtt": ["Function", "Arn"]}}],
            },
        },
        "InvokePermission": {
            "Type": "AWS::Lambda::Permission",
            "Properties": {
                "FunctionName": {"Ref": "Function"},
                "Action": "lambda:InvokeFunction",
                "Principal": "events.amazonaws.com",
                "SourceArn": {"Fn::GetAtt": ["Rule", "Arn"]},
            },
        },
    },
}


# ----------------------------------------------------------------------------
# 输出 YAML
# ----------------------------------------------------------------------------

PLAIN_KEY = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_:.\-]*$")


def scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(v, ensure_ascii=False)   # JSON 的字符串、空列表、空对象都是合法的 YAML


def emit(obj, indent=0):
    pad = "  " * indent
    lines = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = k if PLAIN_KEY.match(k) else json.dumps(k)
            if isinstance(v, Literal):
                lines.append(f"{pad}{key}: |")
                lines += [f"{pad}  {line}" if line.strip() else "" for line in v.splitlines()]
            elif isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{key}:")
                lines += emit(v, indent + 1)
            else:
                lines.append(f"{pad}{key}: {scalar(v)}")
    else:
        for v in obj:
            if isinstance(v, (dict, list)) and v:
                sub = emit(v, indent + 1)
                lines.append(f"{pad}- {sub[0][len(pad) + 2:]}")
                lines += sub[1:]
            else:
                lines.append(f"{pad}- {scalar(v)}")
    return lines


def write(name, template, note):
    text = "\n".join([f"# {note}", "# 本文件由 make_templates.py 生成，不要手动修改。", ""] + emit(template)) + "\n"
    with open(OUT / name, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print(f"已生成 templates/{name}")


def main():
    # IAM 计算内联策略大小时不算空白；用 map-migrated 代入 TagKey 估算
    size = len(json.dumps(MAP_TAGGER_POLICY, separators=(",", ":")).replace('{"Ref":"TagKey"}', '"map-migrated"'))
    if size > INLINE_POLICY_LIMIT:
        raise SystemExit(f"MapTagger 策略 {size} 字符，超过 IAM 内联策略上限 {INLINE_POLICY_LIMIT}")
    pattern = len(json.dumps(realtime_tagger.event_pattern(), separators=(",", ":")))
    if pattern > EVENT_PATTERN_LIMIT:
        raise SystemExit(f"事件模式 {pattern} 字符，超过 EventBridge 上限 {EVENT_PATTERN_LIMIT}")
    OUT.mkdir(exist_ok=True)
    write("map-tagger-roles.yaml", ROLES_TEMPLATE,
          "StackSet map-tagger-roles：部署到整个组织并自动部署，每个账号只部署到一个 region")
    write("map-tagger-realtime.yaml", REALTIME_TEMPLATE,
          "StackSet map-tagger-realtime：部署到整个组织并自动部署，每个账号部署到每个用到的 region（含 us-east-1）")
    print(f"MapTagger 策略 {size} / {INLINE_POLICY_LIMIT} 字符，第二层补扫覆盖 {len(SWEEP_TAG_ACTIONS)} 条打标签权限；"
          f"事件模式 {pattern} / {EVENT_PATTERN_LIMIT} 字符")


if __name__ == "__main__":
    main()
