# 钉钉考勤同步脚本 (DingTalk Attendance Sync)

本项目用于将钉钉考勤数据同步到本地 MySQL 数据库。

## 功能特性
- **批量导出**：使用 `attendance/list` 接口高效获取全员考勤数据。
- **递归获取用户**：自动递归查找所有子部门下的用户，确保人员无遗漏。
- **数据完整性**：自动注入用户姓名（API 原始数据不包含姓名），并存入数据库。
- **数据库同步**：使用 `INSERT ... ON DUPLICATE KEY UPDATE` 策略，防止数据重复。

## 环境要求
- Python 3.x
- MySQL 数据库

## 安装步骤

1.  **安装依赖**：
    ```bash
    pip install -r requirements.txt
    ```

2.  **配置环境**：
    在根目录下创建 `.env` 文件，并填入以下内容：
    ```ini
    # 钉钉应用凭证
    DINGTALK_CLIENT_ID=你的AppKey
    DINGTALK_CLIENT_SECRET=你的AppSecret

    # 数据库配置
    DB_HOST=localhost
    DB_PORT=3306
    DB_USER=root
    DB_PASSWORD=你的数据库密码
    DB_NAME=你的数据库名
    ```

3.  **权限配置**：
    请确保你的钉钉应用在开发者后台已开通以下权限：
    - `qyapi_get_department_list` (通讯录只读权限 - 获取部门)
    - `qyapi_get_department_member` (通讯录只读权限 - 获取成员)
    - 考勤相关读取权限

## 使用方法

**同步上个月的数据（默认行为）：**
```bash
python main.py
```

**同步指定日期范围的数据：**
```bash
python main.py 2024-11-01 2024-11-30
```

## 项目结构
- `main.py`: 程序入口。处理日期范围、数据抓取、转换和存储的主流程。
- `dingtalk_client.py`: 钉钉 API 客户端。处理 Token 管理、部门/用户获取、考勤数据批量抓取。
- `db.py`: 数据库模块。处理连接、自动建表、数据插入/更新。
