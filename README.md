# org-tagger

给 AWS Organizations 里所有成员账号的资源统一补上 `map-migrated` 标签。全部用 Lambda，分两层：

- **实时层**（[realtime_tagger.py](realtime_tagger.py)）：组织里每个成员账号的每个 region 里一个 Lambda。资源一创建就由 CloudTrail 事件触发，几分钟内打上标签。
- **每日扫描**（[sweep_tagger.py](sweep_tagger.py)）：Payer（管理账号）里的一个 Lambda，由 EventBridge Scheduler 每天 02:00 触发。它从 Organizations 列出所有成员账号，assume 进各账号的 `MapTaggerRole`，把实时层漏掉的补上。

之所以要实时层：账单按小时记录资源当时带着的标签，后补的标签不会追溯。只靠每日扫描，资源从创建到被补上标签（最长约一天）这段时间的消费不带标签，这部分拿不到 MAP credit。

默认所有成员账号的资源都打标签。每个账号打什么值，由账号映射表决定：它是 Payer 里的一个 SSM 参数，`"*"` 是所有成员账号的默认值，个别账号可以单独写一行用别的值，值留空就是排除这个账号。Payer 用 RAM 把它共享给组织，两层读的是同一份、规则也一样，所以标签值只在一个地方维护。

部署用 StackSets 部署到整个组织并打开自动部署：新加入组织的账号会自动部署好两个角色和实时层 Lambda，`"*"` 也会自动覆盖它，什么都不用做。被排除的账号里也有这些组件，只是空转。

两层的处理规则一致：

- **默认 dry-run**：`MODE=apply` 之前只写日志，不改动任何资源。
- **不覆盖已有值**：资源上已有同名 key 但值不同时记为 `CONFLICT`，设 `OVERWRITE=true` 才会覆盖。
- **只追加**：只写入这一个标签，不动资源上的其它标签。
- **出错不中断**：单个资源、单个服务或单个账号出错，只在日志里记一行，继续处理其它。

部署步骤见 [DEPLOY.md](DEPLOY.md)：Payer 里的部分在控制台配置一次，成员账号里的部分用 CloudFormation StackSets 部署到整个组织。

## 仓库内容

| 文件 | 说明 |
| --- | --- |
| [realtime_tagger.py](realtime_tagger.py) | 实时层的 Lambda 函数，由 StackSets 部署到每个成员账号的每个 region |
| [sweep_tagger.py](sweep_tagger.py) | 每日扫描的 Lambda 函数，部署在 Payer |
| [templates/map-tagger-roles.yaml](templates/map-tagger-roles.yaml) | StackSet 模板：`MapTaggerRole` 和实时层的执行角色 `MapRealtimeTaggerRole` |
| [templates/map-tagger-realtime.yaml](templates/map-tagger-realtime.yaml) | StackSet 模板：实时层 Lambda 和 EventBridge 规则，代码内联在模板里 |
| [make_templates.py](make_templates.py) | 生成上面两个模板。改了 `realtime_tagger.py` 或权限列表后重新运行 |
| [DEPLOY.md](DEPLOY.md) | 部署与运维：Payer 配置、StackSets 下发、首次上线、常见问题 |

两个函数都是单文件，只用 Lambda 运行时自带的 boto3。扫描函数在 Payer 的控制台里粘贴代码；实时层的代码由 `make_templates.py` 内联进模板，随 StackSet 下发。

`templates/` 下的文件不要手动修改，改 `make_templates.py` 或 `realtime_tagger.py` 后重新生成：

```bash
python make_templates.py
```

## 实时层（realtime_tagger.py）

### 工作原理

1. 有人创建资源（例如 RunInstances、CreateBucket），CloudTrail 记下这次 API 调用，投递到同账号、同 region 的 EventBridge 默认事件总线。
2. EventBridge 规则按事件来源和事件名匹配，触发同 region 的 Lambda。规则只匹配下表里的创建类 API，不含 CreateTags 这类打标签操作，所以 Lambda 自己打标签不会再触发自己。
3. Lambda 用完整 ARN 读取 Payer 共享过来的账号映射表，查出本账号该打的值：有自己的条目就用它，没有就用 `"*"`；映射表缓存 5 分钟。本账号被排除时记一行 `SKIP`，不处理。
4. Lambda 从事件里取出新资源的 ID 或 ARN，读出现有标签，按上面的规则决定记为 `OK`、`CONFLICT`、`WOULD_TAG` 还是打上标签（`TAGGED`）。
5. RunInstances 还会等实例进入 running，再给启动时一起创建的 EBS 卷打标签。这些卷没有单独的 CreateVolume 事件。
6. 资源刚创建时，有的服务会拒绝读写标签。函数每 10 秒重试一次，最多 6 次；还失败就记 `FAILED`，并让这次调用报错，Lambda 默认会再重试两次。最终仍失败的，由第二天的每日扫描补上。

### 覆盖的 API

| 资源类型 | 触发的 API |
| --- | --- |
| `ec2:instance` | RunInstances（连同启动时创建的 EBS 卷，记为 `ec2:volume`） |
| `ec2:volume` | CreateVolume |
| `ec2:snapshot` | CreateSnapshot、CreateSnapshots、CopySnapshot |
| `ec2:image` | CreateImage、CopyImage、RegisterImage（AMI 背后的快照不在内，由每日扫描处理） |
| `ec2:elastic-ip` | AllocateAddress |
| `ec2:natgateway` | CreateNatGateway |
| `ec2:vpc-endpoint` | CreateVpcEndpoint |
| `rds:db` | CreateDBInstance、CreateDBInstanceReadReplica、RestoreDBInstanceFromDBSnapshot、RestoreDBInstanceToPointInTime、RestoreDBInstanceFromS3 |
| `rds:cluster` | CreateDBCluster、RestoreDBClusterFromSnapshot、RestoreDBClusterToPointInTime、RestoreDBClusterFromS3 |
| `rds:snapshot`、`rds:cluster-snapshot` | CreateDBSnapshot、CopyDBSnapshot、CreateDBClusterSnapshot、CopyDBClusterSnapshot |
| `lambda:function` | CreateFunction（CloudTrail 里的事件名是 CreateFunction20150331） |
| `elbv2:loadbalancer`、`elb:loadbalancer` | CreateLoadBalancer（ALB、NLB、GWLB 记为 `elbv2:loadbalancer`；Classic Load Balancer 记为 `elb:loadbalancer`） |
| `dynamodb:table` | CreateTable、RestoreTableFromBackup、RestoreTableToPointInTime、ImportTable |
| `efs:file-system` | CreateFileSystem |
| `elasticache:cluster`、`elasticache:replication-group` | CreateCacheCluster、CreateReplicationGroup |
| `eks:cluster` | CreateCluster |
| `s3:bucket` | CreateBucket |
| `cloudfront:distribution` | CreateDistribution、CreateDistributionWithTags（全局服务，事件只出现在 us-east-1） |
| `sqs:queue` | CreateQueue |
| `sns:topic` | CreateTopic |
| `kms:key` | CreateKey |
| `dms:replication-instance`、`dms:replication-config` | CreateReplicationInstance、CreateReplicationConfig（DMS Serverless） |
| `kafka:cluster` | CreateCluster、CreateClusterV2（MSK，含 Serverless） |
| `route53:hostedzone`、`route53:healthcheck` | CreateHostedZone、CreateHealthCheck（全局服务，事件只出现在 us-east-1） |
| `route53resolver:resolver-endpoint`、`route53resolver:resolver-rule` | CreateResolverEndpoint、CreateResolverRule |
| `workspaces:workspace` | CreateWorkspaces |
| `memorydb:cluster` | CreateCluster（MemoryDB） |
| `mq:broker` | CreateBroker |
| `docdb-elastic:cluster` | CreateCluster（DocumentDB Elastic Clusters） |
| `ds:directory` | CreateDirectory、CreateMicrosoftAD、ConnectDirectory |
| `es:domain` | CreateDomain、CreateElasticsearchDomain（OpenSearch Service） |

普通的 Amazon DocumentDB 和 Neptune 走的是 RDS 的 API，已包含在 `rds:db`、`rds:cluster` 里。不在表里的资源类型，由每日扫描的第二层（Tagging API 补扫）兜底。

EventBridge 规则的事件模式由代码里的 `EVENTS` 表生成，`make_templates.py` 会把它写进模板。想单独看的话，本地运行：

```bash
python realtime_tagger.py
```

### 环境变量

由 StackSet `map-tagger-realtime` 的参数设置，一般不用手动改。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MAPPING_PARAM` | 必填 | 账号映射表 SSM 参数的完整 ARN（模板参数 `MappingParameterArn`） |
| `TAG_KEY` | `map-migrated` | 标签 key（模板参数 `TagKey`） |
| `MODE` | `dryrun` | `dryrun` 只写日志；`apply` 才真正打标签（模板参数 `Mode`） |
| `LOG_RETENTION_DAYS` | `180` | 本函数日志组的保留天数（模板参数 `LogRetentionDays`）。日志组是函数第一次运行时自动建的，模板管不到，所以由函数在每个执行环境第一次运行时自己设置；没设这个变量就不改动日志组 |
| `TAG_VALUE` | 无 | 可选。填了就直接用这个值，不查映射表，一般只在单独测试时用 |
| `OVERWRITE` | `false` | `true` 时覆盖已有的不同值 |

## 每日扫描（sweep_tagger.py）

### 工作原理

同一个 Lambda 函数 `map-daily-sweep` 有两种调用：

1. **调度**：Scheduler 每天 02:00 调用它，事件里没有账号信息。函数从 SSM 参数读出账号映射表；表里有 `"*"` 时，从 Organizations 列出所有 ACTIVE 成员账号（不含 Payer），再加上表里单独写的账号，去掉值为空的。然后对每个账号 assume 进 `MapTaggerRole`，取得已启用的 region（或用环境变量 `REGIONS` 指定），然后按“账号 × region”异步调用自己，每个账号另加一次 `global`。assume 失败的账号记一行 `ERROR`，不发起扫描。
2. **扫描**：每次调用只处理一个账号的一个 region。先 assume 进该账号的 `MapTaggerRole`，再分两层枚举资源，比较现值，按上面的规则处理。`global` 那次处理 S3、CloudFront 和 Route 53。某个服务没在这个 region 上线（接口域名解析不了）时直接跳过，不记 `ERROR`。
   - **第一层：服务原生 API。** 能找到从没打过任何标签的资源，覆盖的类型见下表。
   - **第二层：Resource Groups Tagging API 补扫。** 用 `GetResources` 覆盖第一层没写到的各种类型，但它只返回“当前有标签或曾经打过标签”的资源。第一层处理过的资源会跳过，不重复记录；S3、CloudFront、Route 53 由 `global` 那次处理，这里也跳过。CloudFormation 栈也会跳过：栈本身不产生费用，经 Tagging API 给栈打标签会走一次栈更新，把标签传到栈里所有资源，StackSets 管理的栈还会和 StackSet 对不上。预算（budgets）和付款方式（payments）同样不产生费用，Tagging API 也打不上，一并跳过。环境变量 `NO_SWEEP=true` 可以关掉这一层。

拆成这么多次调用，是因为 Lambda 单次调用最长 15 分钟，整个组织放在一次调用里可能跑不完；每次只处理一个 region，大账号也来得及。同时跑多少个，由函数的预留并发控制。

第二层补扫找到的资源可能来自任何服务，所以 `MapTaggerRole` 里放了约 320 个服务的打标签权限。这批权限是用 AWS 策略生成器的服务列表整理出来的（见 `make_templates.py` 的 `SWEEP_TAG_ACTIONS`），去掉了 iam、sts、organizations 这类和费用无关、又涉及权限的服务，并且加了条件：只能写 `map-migrated` 这一个 key。

Payer 自己的资源也通过 `MapTaggerRole` 处理，但 `"*"` 不包括 Payer：如果 Payer 里也有要打标签的资源，就在 Payer 里用普通 CloudFormation 栈部署同样的模板，并在映射表里单独写一行 Payer。

### 覆盖的资源类型

| 范围 | 日志里的 type | 说明 |
| --- | --- | --- |
| global | `s3:bucket` | 在桶所在 region 用 Tagging API 追加标签。不用 `PutBucketTagging`，因为它会整体覆盖桶的标签集，也写不回 CloudFormation 留下的 `aws:` 前缀标签 |
| global | `cloudfront:distribution` | |
| global | `route53:hostedzone`、`route53:healthcheck` | |
| Region | `ec2:instance` | 不含 terminated、shutting-down 状态的实例 |
| Region | `ec2:volume` | |
| Region | `ec2:snapshot` | 只含本账号拥有的快照 |
| Region | `ec2:image` | 只含本账号拥有的 AMI |
| Region | `ec2:elastic-ip` | 只含 VPC 弹性 IP（有 AllocationId 的） |
| Region | `ec2:natgateway` | 只含 pending、available 状态 |
| Region | `ec2:vpc-endpoint` | 不含 deleting、deleted、rejected、failed、expired 状态 |
| Region | `rds:db`、`rds:cluster` | |
| Region | `rds:snapshot`、`rds:cluster-snapshot` | 只含手动快照 |
| Region | `lambda:function` | |
| Region | `elbv2:loadbalancer` | ALB、NLB、GWLB |
| Region | `elb:loadbalancer` | Classic Load Balancer |
| Region | `dynamodb:table` | |
| Region | `efs:file-system` | |
| Region | `elasticache:cluster` | |
| Region | `eks:cluster` | |
| Region | `sqs:queue` | |
| Region | `sns:topic` | |
| Region | `kms:key` | 只含客户托管的密钥；AWS 托管的密钥不能打标签，等待删除、等待导入的也跳过 |
| Region | `dms:replication-instance`、`dms:replication-config` | 复制实例和 DMS Serverless 复制配置。DMS Serverless 没在部分较新的 region 上线，那里调用会返回 AccessDenied，直接跳过 |
| Region | `kafka:cluster` | MSK，含 Serverless |
| Region | `route53resolver:resolver-endpoint`、`route53resolver:resolver-rule` | 规则不含 AWS 自带的和别的账号共享过来的 |
| Region | `workspaces:workspace` | 不含 terminating、terminated 状态 |
| Region | `memorydb:cluster` | |
| Region | `mq:broker` | |
| Region | `docdb-elastic:cluster` | DocumentDB Elastic Clusters；普通 DocumentDB 包含在 `rds:cluster` 里 |
| Region | `ds:directory` | 不含别的账号共享过来的目录 |
| Region | `es:domain` | OpenSearch Service 域，不含已删除的 |
| Region | `<服务名> (sweep)` | 第二层补扫找到的其它类型 |

### 账号映射表

存在 Payer 的 SSM 参数 `/map-tagger/accounts` 里，是一个 JSON 对象，两层都按同样的规则读：

```json
{
  "*": "migXXXXXXXXXX",
  "444455556666": "migYYYYYYYYYY",
  "777788889999": ""
}
```

| key | 含义 |
| --- | --- |
| `"*"` | 所有 ACTIVE 成员账号的默认值，不包括 Payer。以后新加入组织的账号自动包含在内 |
| 12 位账号 ID | 这个账号用自己的值，覆盖 `"*"`。Payer 要处理就必须这样单独写一行 |
| 账号 ID，值为 `""` | 排除这个账号，两层都不处理 |

没有 `"*"` 时，只处理表里单独写的账号，首次上线时就先这样只放一个测试账号。

要用高级参数（Advanced tier），因为只有高级参数能用 RAM 共享给其它账号。高级参数上限 8 KB，表里只写例外的账号，一般用不完。

### 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MAPPING_PARAM` | `/map-tagger/accounts` | 账号映射表所在的 SSM 参数名 |
| `ROLE_NAME` | `MapTaggerRole` | 各账号里被 assume 的角色名 |
| `TAG_KEY` | `map-migrated` | 标签 key |
| `MODE` | `dryrun` | `dryrun` 只写日志；`apply` 才真正打标签 |
| `OVERWRITE` | `false` | `true` 时覆盖已有的不同值 |
| `REGIONS` | 每个账号已启用的全部 region | 只处理这些 region，逗号分隔，如 `ap-southeast-1,ap-southeast-5,us-east-1` |
| `NO_SWEEP` | `false` | `true` 时不跑第二层 Tagging API 补扫 |

### 手动测试

在 Lambda 控制台的 Test 页签，用下面的测试事件只扫一个账号的一个 region，结果里会返回各 action 的数量：

```json
{"account": "111122223333", "region": "ap-southeast-1"}
```

`region` 填 `global` 处理该账号的 S3 和 CloudFront。测试事件填 `{}` 就是一次完整运行，和 Scheduler 触发的效果一样。

## 日志

两个函数每处理一个资源，都往各自的 CloudWatch 日志组写一行 JSON：

| 函数 | 日志组 | 保留期 |
| --- | --- | --- |
| 实时层 | `/aws/lambda/map-realtime-tagger`（在各成员账号的各 region） | 由函数自己按 `LOG_RETENTION_DAYS` 设置，默认 180 天 |
| 每日扫描 | `/aws/lambda/map-daily-sweep`（在 Payer） | 在 CloudWatch 控制台手动设置，见 [DEPLOY.md](DEPLOY.md#设置每天定时运行) |

| 字段 | 说明 |
| --- | --- |
| `account_id` | 资源所在账号 |
| `region` | 资源所在 region；`global` 表示全局资源；`-` 表示调度时的账号级错误 |
| `event` | 只有实时层有：触发的 API |
| `type` | 资源类型；`ERROR` 行里是出错的环节：处理函数名（如 `h_lambda`）、`assume_role`、`mapping` 或 `invoke` |
| `resource` | 资源 ID 或 ARN |
| `current_value` | 该 key 在资源上的现值，没有则为空 |
| `action` | 见下表 |
| `detail` | 出错或冲突时的说明 |

| action | 含义 |
| --- | --- |
| `OK` | 已经是正确的值 |
| `WOULD_TAG` | dry-run 时表示会被打上 |
| `TAGGED` | 本次已打上 |
| `CONFLICT` | 已有同名 key 但值不同，没有改动 |
| `FAILED` | 单个资源打标签失败 |
| `ERROR` | 整个服务或整个账号出错，例如 assume role 失败、服务在该 region 不可用、被 SCP 拒绝 |
| `SKIP` | 只有实时层有：事件不在处理范围，或者本账号在映射表里被排除。规则按事件来源和事件名分别匹配，会混进别的服务的同名事件，例如 ElastiCache 的 CreateSnapshot，属于正常情况 |
| `NO_ID` | 只有实时层有：事件在处理范围，但没找到资源 ID。`detail` 里附了事件原文，多半是事件格式和预期不同 |
| `DISPATCHED` | 只有每日扫描有：调度完成，`detail` 里是账号数和发起的扫描次数 |
| `WARN` | 只有实时层有：没能设置日志组的保留期，例如执行角色还没更新到有 `logs:PutRetentionPolicy` 权限的版本。不影响打标签，下次运行会再试 |

在 CloudWatch Logs Insights 里选对应的日志组，用下面的查询看需要关注的记录：

```
fields @timestamp, account_id, region, action, type, resource, current_value, detail
| filter ispresent(action) and action not in ["OK", "TAGGED", "SKIP", "DISPATCHED"]
| sort @timestamp desc
| limit 10000
```

按账号和 action 汇总：

```
filter ispresent(action)
| stats count(*) as n by account_id, action
```

每种情况怎么处理，见 [DEPLOY.md 的“审日志”](DEPLOY.md#审日志)。

## 增加资源类型

**实时层**：在 `realtime_tagger.py` 的 `EVENTS` 表里加一行，key 是 `(eventSource, eventName)`，value 是从事件里取出资源 ID 的函数。资源 ID 在响应里的用 `from_resp`，要从请求参数拼 ARN 的用 `from_req`。如果是新服务，还要在 `ADAPTERS` 里加一对读标签、打标签的函数，并把对应权限加进 `make_templates.py` 的 `REALTIME_READ_ACTIONS` 和 `REALTIME_TAG_ACTIONS`。改完后运行 `python make_templates.py`，再按 [DEPLOY.md](DEPLOY.md#5-日常运维与常见问题) 里“更新模板”的方法更新两个 StackSet。

**每日扫描**：某类资源如果从没打过任何标签，又不在第一层的类型里，补扫找不到它。这时可以照现有的 `h_xxx` 函数加一个，再放进 `REGIONAL_HANDLERS`（按 region 跑）或 `GLOBAL_HANDLERS`（每个账号只跑一次）。例如 SNS 主题：

```python
def h_sns(ctx):
    sns = ctx.client("sns")
    for t in paginate(sns, "list_topics", "Topics"):
        arn = t["TopicArn"]
        tags = tdict(sns.list_tags_for_resource(ResourceArn=arn).get("Tags"))
        yield Res("sns:topic", arn, tags,
                  lambda arn=arn: sns.tag_resource(ResourceArn=arn, Tags=ctx.tagset()))
```

写的时候注意：

- `Res` 的资源 ID 用 ARN，或 ARN 的最后一段（如 EC2 的 `i-xxx`），第二层补扫才能认出它已处理过，不会重复记录。
- 打标签的 lambda 要用默认参数绑定变量（`lambda arn=arn: ...`），否则循环里所有闭包都会指向最后一个资源。
- 新加的 List/Describe 权限加进 `make_templates.py` 的 `SWEEP_READ_ACTIONS`（打标签权限一般已在 `SWEEP_TAG_ACTIONS` 里），重新生成模板后更新 StackSet `map-tagger-roles`，再在 Payer 里更新扫描 Lambda 的代码。

## 限制与注意事项

- 仅供参考，请先在测试账号验证后再用于生产。
- 只支持 `aws` 标准分区，不含中国区（`aws-cn`）等其它分区。
- 依赖 Organizations 的全部功能：服务托管的 StackSets 和 RAM 共享给组织都需要它。
- 实时层依赖 CloudTrail：账号里要有一条正在记录管理事件的 trail，否则 EventBridge 收不到 API 事件，实时层完全不工作。Payer 建的组织级 trail 就够了，已经实际验证过能触发成员账号里的规则。
- 实时层按 region 部署：没部署的 region 里创建的资源，只能等每日扫描。CloudFront 的事件只出现在 us-east-1，所以 us-east-1 一定要部署。StackSet 里如果选了 opt-in region，没启用它的账号的实例会 FAILED，属正常。
- 映射表的 RAM 共享是按 region 的，要建在参数所在的 region，否则实时层读映射表会报 AccessDeniedException。
- StackSets 部署到整个组织，被排除的账号里也会有两个角色和实时层 Lambda，只是空转。连这些资源都不能放的账号，要把部署目标改成按 OU 并避开它们所在的 OU（StackSets 的自动部署不认账号过滤，没法按账号排除）。
- 服务托管的 StackSets 不会部署到管理账号，Payer 自己的资源要用普通 CloudFormation 栈单独部署。
- 已在真实组织里部署并验证：StackSets 下发、实时层用 CreateVolume 走通了完整链路、每日扫描跑通了全量 dry-run。实时层其它 API 的事件解析是按 CloudTrail 的事件格式写的，只用模拟事件测试过；日志里出现 `NO_ID` 说明格式和预期不同。SQS、SNS、KMS 等后来新增的服务，两层都只用模拟数据和 botocore 的接口校验测试过，还没在真实账号里跑过。
- 每日扫描连不上某个 region（例如那个 region 本身出问题）时，那里会记 `ERROR`，那次扫描调用还会一直等到 15 分钟上限被强制结束，不影响其它 region。
- `MapTaggerRole` 第二层的打标签权限是按 AWS 策略生成器当时的服务列表整理的。以后 AWS 新增的服务不在列表里，遇到 AccessDenied 时再补。
- 实时层只是缩短空档。最可靠的仍然是创建资源时就带上标签：启动模板、Terraform 的 `default_tags`、Auto Scaling 组的 `PropagateAtLaunch=true` 等。
- 由 Terraform、CloudFormation 管理的资源，下次部署时可能把打上的标签抹掉，要同步把标签写进 IaC 代码。
