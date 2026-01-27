# 钉钉审批流程同步工具 (DingTalk Approval Sync)

这是一个基于 Python 开发的钉钉审批数据同步工具，支持将审批实例同步到本地 MySQL 数据库。
本工具支持 **实时流同步** (基于 DingTalk Stream SDK) 和 **历史数据下载** (基于批量 API)。

## 功能特性

- **数据完整性**:
  - **自动关联姓名**: 自动将 `UserID` 转换为姓名 (发起人、当前审批人)。
  - **当前审批人**: 对于 `RUNNING` (进行中) 的审批单，自动解析当前需要审批的人员姓名。
  - **完整表单**: 自动保存完整的表单组件数据 (`form_component_values` JSON格式)。
- **双模式同步**:
  - **实时流**: 毫秒级延迟监听数据变更。
  - **历史补录**: 支持按日期范围批量下载。
- **本地缓存**: 引入 `dingtalk_user` 表缓存企业通讯录，大幅减少 API 调用并提高解析速度。
- **多模板支持**: `.env` 配置支持多个 `PROCESS_CODE`，批量同步不同类型的审批流。

## 环境要求

- Python 3.8+
- MySQL 5.7+
- 钉钉应用凭证 (Client ID & Secret)

## 安装部署

1. 安装依赖:
   ```bash
   pip install -r requirements.txt
   ```

2. 配置 `.env` 文件:
   ```properties
   DINGTALK_CLIENT_ID=your_app_key
   DINGTALK_CLIENT_SECRET=your_app_secret
   DB_HOST=localhost
   DB_USER=root
   DB_PASSWORD=password
   DB_NAME=your_db
   
   # 审批模板唯一标识 (Process Code)，支持多个用逗号分隔
   PROCESS_CODE=PROC-XXXX,PROC-YYYY # 备注
   ```

## 使用手册

### 第一步：同步全员名单 (必做)
为了能显示“张三”而不是 `user123`，你需要先将公司员工列表同步到本地数据库。
**原理**：程序会遍历钉钉通讯录，将 UserID 和 姓名 存入本地 `dingtalk_user` 表。
```bash
python main.py sync-users
```
*建议：每周或有新员工入职时运行一次。*

### 第二步：查找审批模板 Code
如果你不知道 `.env` 里该填什么，运行这个查看所有可见的模板：
```bash
python main.py list-codes
```

### 第三步：开始同步
#### 方式 A：同步历史数据
下载过去的数据。
```bash
# 默认同步上个月
python main.py history

# 指定日期范围
python main.py history 2024-01-01 2024-01-31
```
**数据逻辑说明**：
- 程序从钉钉 API 获取原始 JSON。
- 解析出 `originator_userid` (发起人ID) 和 `tasks` (任务列表)。
- **发起人姓名**：用 ID 去本地 `dingtalk_user` 表查姓名。
- **当前审批人**：遍历 `tasks`，找到状态为 `RUNNING` 的任务，提取其 UserID，再去本地表查姓名。
- **最终存储**：将解析出的姓名分别存入 `process_instance` 表的 `originator_name` 和 `current_approvers` 字段。

#### 方式 B：实时监听 (推荐)
启动一个后台进程，实时接收钉钉的推送。
```bash
python main.py stream
```

#### 方式 C：数据清洗 (ETL)
本工具内置了数据清洗功能，可以将复杂的表单组件数据 (`form_component_values`) 转换为易读的 JSON 格式 (`form_values_cleaned`)。
- **自动清洗**：使用上述 `stream` 或 `history` 模式同步时，程序会自动清洗数据并保存。
- **手动全量清洗**：如果需要重新清洗已有数据，可运行：
  ```bash
  python etl.py
  ```

## 数据库结构

### 1. `process_instance` (审批主表)
核心业务数据。

| 字段名 | 说明 | 来源 |
| :--- | :--- | :--- |
| `process_instance_id` | 审批实例ID | API 原始数据 |
| `title` | 标题 (如: 张三的请假) | API 原始数据 |
| `status` | 状态 (NEW, RUNNING, COMPLETED, TERMINATED) | API 原始数据 |
| `result` | 结果 (agree, refuse) | API 原始数据 |
| `originator_name` | **发起人姓名** | 本地 `dingtalk_user` 关联查找 |
| `current_approvers` | **当前审批人** (逗号分隔) | API `tasks` 解析 + 本地关联查找 |
| `form_component_values` | 完整表单数据 (JSON) | API 原始数据 |
| `form_values_cleaned` | **已清洗表单数据** (简易JSON) | ETL 自动生成 |
| `create_time` | 创建时间 | API 原始数据 |

### 2. `dingtalk_user` (用户缓存表)
用于 ID 转 姓名。

| 字段名 | 说明 |
| :--- | :--- |
| `userid` | 钉钉 User ID |
| `name` | 姓名 |
