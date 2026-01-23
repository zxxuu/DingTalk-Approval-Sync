# DingTalk Approval Process Sync

This repository contains a tool to synchronize DingTalk Approval Process data to a local MySQL database.
It supports **Real-time Sync** (Stream Mode) and **Historical Data Sync** (Batch Mode).

## Key Features

- **Data Enrichment**:
  - **Name Resolution**: Automatically converts User IDs to Names (Originator & Current Approvers).
  - **Current Approvers**: Parses `RUNNING` tasks to identify who is currently holding up the approval.
  - **Full Form Data**: Saves complete form component values as JSON.
- **Dual Sync Modes**:
  - **Stream**: Real-time listeners for immediate updates.
  - **History**: Batch download for past records.
- **Local Cache**: Uses a local `dingtalk_user` table to cache employee info, reducing API limits.
- **Multi-template**: Supports syncing multiple process codes simultaneously.

## Prerequisites

- Python 3.8+
- MySQL 5.7+
- DingTalk App Credentials

## Installation

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Configure `.env`:
   ```properties
   DINGTALK_CLIENT_ID=your_app_key
   DINGTALK_CLIENT_SECRET=your_app_secret
   DB_HOST=localhost
   DB_USER=root
   DB_PASSWORD=password
   DB_NAME=your_db
   
   PROCESS_CODE=PROC-XXXX,PROC-YYYY
   ```

## User Guide

### Step 1: Sync User Directory (Required)
To display names (e.g., "John Doe") instead of IDs, you must sync the company directory to the local database first.
```bash
python main.py sync-users
```
*Tip: Run this weekly to keep the list updated.*

### Step 2: Find Process Codes
List all approval templates visible to you:
```bash
python main.py list-codes
```

### Step 3: Sync Data
#### A. History Mode
Download past data.
```bash
# Default (Last Month)
python main.py history

# Custom Range
python main.py history 2024-01-01 2024-01-31
```
**How Data is Processed**:
1. Fetch raw JSON from DingTalk API.
2. Extract `originator_userid` and the `tasks` list.
3. **Originator Name**: Look up ID in local `dingtalk_user` table.
4. **Current Approvers**: Scan `tasks` for `RUNNING` status, extract User IDs, and look up names in local table.
5. **Storage**: Save resolved names into `originator_name` and `current_approvers` columns in MySQL.

#### B. Stream Mode
Listen for real-time events.
```bash
python main.py stream
```

## Database Schema

### `process_instance`
Main approval records.

| Field | Description | Source |
| :--- | :--- | :--- |
| `process_instance_id` | Unique ID | API Raw |
| `title` | Title | API Raw |
| `status` | Status (RUNNING, COMPLETED...) | API Raw |
| `result` | Result (agree, refuse...) | API Raw |
| `originator_name` | **Originator Name** | Derived (User Cache) |
| `current_approvers` | **Current Approvers** | Derived (Tasks + User Cache) |
| `form_component_values` | Form Data (JSON) | API Raw |

### `dingtalk_user`
Local user cache.

| Field | Description |
| :--- | :--- |
| `userid` | DingTalk UserID |
| `name` | User Name |
