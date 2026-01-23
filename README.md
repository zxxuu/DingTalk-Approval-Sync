# DingTalk Attendance Sync Script

This project synchronizes DingTalk attendance data to a local MySQL database.

## Features
- **Batch Export**: Efficiently fetches attendance data for all users using the `attendance/list` API.
- **Recursive User Fetching**: Automatically finds all users in all sub-departments.
- **Data Completeness**: Injects user names into the attendance records (as the API does not provide them).
- **Database Sync**: Upserts records to MySQL, preventing duplicates.

## Prerequisites
- Python 3.x
- MySQL Database

## Installation

1.  **Install Dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

2.  **Configuration**:
    Create a `.env` file in the root directory with the following content:
    ```ini
    # DingTalk Credentials
    DINGTALK_CLIENT_ID=your_app_key
    DINGTALK_CLIENT_SECRET=your_app_secret

    # Database Credentials
    DB_HOST=localhost
    DB_PORT=3306
    DB_USER=root
    DB_PASSWORD=your_password
    DB_NAME=your_database_name
    ```

3.  **Permissions**:
    Ensure your DingTalk application has the following permissions:
    - `qyapi_get_department_list` (通讯录只读权限)
    - `qyapi_get_department_member` (通讯录只读权限)
    - Attendance read permissions

## Usage

**Sync Last Month's Data (Default):**
```bash
python main.py
```

**Sync Specific Date Range:**
```bash
python main.py 2024-11-01 2024-11-30
```

## Project Structure
- `main.py`: Entry point. Handles date range, fetching, transformation, and saving.
- `dingtalk_client.py`: Handles DingTalk API interactions (Token, Departments, Users, Attendance).
- `db.py`: Handles Database connection, table creation, and record upsert.
