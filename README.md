# org-tagger

给 AWS Organizations 里所有账号的资源统一补一个标签。脚本在管理账号（Payer）上运行，逐个 assume 成员账号里的 `OrganizationAccountAccessRole`，给缺少该标签的资源补上，并输出 CSV 报告。

主要用于 MAP（Migration Acceleration Program）项目的 `map-migrated` 标签，也适用于其它“全组织统一打一个标签”的场景。

- **默认 dry-run**：不加 `--apply` 不会改动任何资源，只出报告。
- **不覆盖已有值**：资源上已有同名 key 但值不同时记为 `CONFLICT`，加 `--overwrite` 才覆盖。
- **只追加**：只写入这一个标签，不动资源上的其它标签。
- **出错不中断**：单个资源、单个服务或单个账号出错，只在报告里记一行，继续处理其它。

部署到 Payer 的 EC2 上每天定时运行的完整步骤见 [DEPLOY.md](DEPLOY.md)。

## 仓库内容

| 文件 | 说明 |
| --- | --- |
| [org_tagger.py](org_tagger.py) | 脚本本体，单文件，只依赖 boto3 |
| [DEPLOY.md](DEPLOY.md) | 部署与运维：成员账号角色、Payer 上的 EC2 和每日定时任务、首次上线流程、常见问题 |

## 工作原理

1. 用当前凭证确认管理账号 ID，从 Organizations 列出所有 `ACTIVE` 账号，再按 `--accounts` / `--exclude-accounts` 过滤。
2. 多个账号并行处理（`--workers`，默认 8）。管理账号自己直接用当前凭证；成员账号 assume `--role-name` 指定的角色。每处理一个 region 都重新 assume 一次，避免长时间运行时 1 小时的临时凭证过期。
3. 每个账号先处理全局资源（S3、CloudFront，只跑一次），再逐个处理 region（默认是该账号已启用的全部 region）。
4. 每个 region 分两层枚举资源：
   - **第一层：服务原生 API。** 能找到从没打过任何标签的资源，覆盖的类型见下表。
   - **第二层：Resource Groups Tagging API 补扫。** 用 `GetResources` 覆盖第一层没写到的类型（SNS、SQS、KMS、日志组等），但它只返回“当前有标签或曾经打过标签”的资源。第一层处理过的资源会跳过，不重复记录。用 `--no-sweep` 可以关掉这一层。
5. 对每个资源比较该 key 的现值，记为 `OK`、`CONFLICT`、`WOULD_TAG`、`TAGGED` 或 `FAILED`，写入报告。

## 覆盖的资源类型

| 范围 | 报告里的 type | 说明 |
| --- | --- | --- |
| 全局 | `s3:bucket` | 在桶所在 region 用 Tagging API 追加标签。不用 `PutBucketTagging`，因为它会整体覆盖桶的标签集，也写不回 CloudFormation 留下的 `aws:` 前缀标签 |
| 全局 | `cloudfront:distribution` | |
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
| Region | `elbv2:loadbalancer` | ALB、NLB、GWLB；不含 Classic Load Balancer |
| Region | `dynamodb:table` | |
| Region | `efs:file-system` | |
| Region | `elasticache:cluster` | |
| Region | `eks:cluster` | |
| Region | `<服务名> (sweep)` | 第二层补扫找到的其它类型 |

## 快速开始

需要 Python 3.9+ 和 boto3，并使用**管理账号**的凭证运行（只有管理账号能直接调用 `organizations:ListAccounts`）。脚本调用 STS、Organizations 时用当前凭证配置的默认 region（例如 `AWS_DEFAULT_REGION`），没配置时用 `us-east-1`。

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install boto3
```

建议按下面的顺序来：

```bash
# 1) 挑一个账号、一个 region 试跑（dry-run）
python org_tagger.py --tag-key map-migrated --tag-value migXXXXXXXXXX \
    --accounts 111122223333 --regions ap-southeast-1 --output test.csv

# 2) 全组织 dry-run，审一遍报告
python org_tagger.py --tag-key map-migrated --tag-value migXXXXXXXXXX

# 3) 确认无误后正式执行
python org_tagger.py --tag-key map-migrated --tag-value migXXXXXXXXXX --apply
```

脚本只适合手动跑一两次时这样用。要长期每天补标签，按 [DEPLOY.md](DEPLOY.md) 部署到 Payer 的 EC2 上。

## 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--tag-key` | 必填 | 标签 key，MAP 项目固定为全小写 `map-migrated` |
| `--tag-value` | 必填 | 标签 value，MAP 项目为 MPE 值（如 `migXXXXXXXXXX`） |
| `--role-name` | `OrganizationAccountAccessRole` | 成员账号里被 assume 的角色名 |
| `--accounts` | 全部 ACTIVE 账号 | 只处理这些账号 ID，空格分隔 |
| `--exclude-accounts` | 无 | 跳过这些账号 ID，空格分隔 |
| `--regions` | 每个账号已启用的全部 region | 只处理这些 region，空格分隔 |
| `--no-sweep` | 关 | 不跑第二层 Tagging API 补扫 |
| `--overwrite` | 关 | 已有同名 key 但值不同时覆盖 |
| `--apply` | 关 | 真正写标签；不加只做 dry-run |
| `--workers` | `8` | 并行处理的账号数 |
| `--output` | `tagging_report.csv` | 报告路径 |

## 报告

报告是 CSV，编码 UTF-8 with BOM，Excel 直接打开中文不乱码。每处理一个资源就写一行，中途中断时已写出的部分也能看。运行结束时日志会打印各 action 的数量。

| 列 | 说明 |
| --- | --- |
| `account_id`、`account_name` | 资源所在账号 |
| `region` | 资源所在 region；`global` 表示全局资源；`-` 表示账号级错误 |
| `type` | 资源类型；`ERROR` 行里是出错的处理函数名（如 `h_lambda`）或 `assume_role` |
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

每种情况怎么处理，见 [DEPLOY.md 的“审报告”](DEPLOY.md#审报告)。

## 增加资源类型

某类资源如果从没打过任何标签，又不在第一层的类型里，补扫找不到它。这时可以照现有的 `h_xxx` 函数加一个，再放进 `REGIONAL_HANDLERS`（按 region 跑）或 `GLOBAL_HANDLERS`（每个账号只跑一次）。例如 SNS 主题：

```python
def h_sns(ctx):
    sns = ctx.client("sns")
    for t in paginate(sns, "list_topics", "Topics"):
        arn = t["TopicArn"]
        tags = tdict(sns.list_tags_for_resource(ResourceArn=arn).get("Tags"))
        yield Res("sns:topic", arn, tags,
                  lambda arn=arn: sns.tag_resource(ResourceArn=arn, Tags=ctx.tagset()))
```

写的时候注意两点：

- `Res` 的资源 ID 用 ARN，或 ARN 的最后一段（如 EC2 的 `i-xxx`），第二层补扫才能认出它已处理过，不会重复记录。
- 打标签的 lambda 要用默认参数绑定变量（`lambda arn=arn: ...`），否则循环里所有闭包都会指向最后一个资源。

如果脚本部署在 EC2 上并且要处理 Payer 自己的资源，还要把对应的 List/Describe 和打标签权限加进实例角色的策略，见 [DEPLOY.md](DEPLOY.md#42-创建-ec2-实例角色)。

## 限制与注意事项

- 仅供参考，请先在测试账号验证后再用于生产。
- 只支持 `aws` 标准分区，不含中国区（`aws-cn`）等其它分区。
- 每个成员账号都要有同名角色。Organizations 新建的账号自带 `OrganizationAccountAccessRole`；邀请加入的账号通常没有，需要由对方管理员建一次，见 [DEPLOY.md](DEPLOY.md#3-成员账号创建角色)。
- `OrganizationAccountAccessRole` 是管理员权限。能运行这个脚本的身份可以进入所有成员账号，相当于持有全组织的管理员权限，要严格控制谁能使用它。
- 由 Terraform、CloudFormation 管理的资源，下次部署时可能把脚本打的标签抹掉，要同步把标签写进 IaC 代码。
