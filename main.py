import sys
import logging
import os
import json
import time
from datetime import datetime, date, timedelta
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv

# Local modules
from db import (
    create_table_if_not_exists, 
    upsert_process_instance, 
    upsert_dingtalk_users, 
    get_user_name_from_db
)
from dingtalk_client import DingTalkClient

# DingTalk Stream SDK
from dingtalk_stream import DingTalkStreamClient, Credential

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

# Global Client
dt_client = DingTalkClient()

def get_last_month_range():
    """Get the start and end date of the previous month."""
    today = date.today()
    last_month = today - relativedelta(months=1)
    start_date = last_month.replace(day=1)
    next_month = last_month + relativedelta(months=1)
    end_date = next_month.replace(day=1) - timedelta(days=1)
    return start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')

def get_user_name_cached(userid):
    """
    Get user name, try cache first. 
    Note: For now we only read DB. Real-time fetch could be added if needed.
    """
    if not userid:
        return None
    name = get_user_name_from_db(userid)
    return name if name else userid # Fallback to ID if name not found

def transform_process_instance(instance_data, forced_id=None):
    """
    Transform API process instance detail to DB record format.
    Flatten the structure where necessary.
    """
    if not instance_data:
        return None

    # Helper to get value from either snake_case or camelCase
    def get_val(keys):
        if isinstance(keys, str):
            keys = [keys]
        for k in keys:
            if k in instance_data:
                return instance_data[k]
        return None

    # Extract form component values
    form_values = instance_data.get('form_component_values') or instance_data.get('formComponentValues') or []
    
    pid = get_val(['process_instance_id', 'processInstanceId']) or forced_id
    
    originator_userid = get_val(['originator_userid', 'originatorUserId'])
    originator_name = get_user_name_cached(originator_userid)
    
    # Extract current approvers
    # Tasks structure: "tasks": [ { "userid": "...", "status": "RUNNING" } ]
    tasks = instance_data.get('tasks', [])
    current_approver_ids = set()
    
    # Debug: Check if we have running tasks
    # has_running = False
    
    for t in tasks:
        # Check standard status field (usually 'status' or 'task_status')
        # API usually returns 'task_status' for detailed tasks
        status = (t.get('task_status') or t.get('status') or '').upper()
        if status == 'RUNNING':
            # has_running = True
            uid = t.get('userid')
            if uid:
                current_approver_ids.add(uid)
            else:
                logger.warning(f"Found RUNNING task but no userid: {t}")
    
    # if not current_approver_ids and has_running:
    #    logger.warning(f"Running tasks found but no approvers extracted. Tasks Dump: {json.dumps(tasks, ensure_ascii=False)}")
    
    current_approver_names = []
    for uid in current_approver_ids:
        name = get_user_name_cached(uid)
        current_approver_names.append(name)

    current_approvers_str = ",".join(current_approver_names) if current_approver_names else None

    # Debug log for current approvers logic
    # logger.info(f"Instance {pid} Status: {get_val('status')} | Found RUNNING tasks: {len(current_approver_ids)} | Approvers: {current_approvers_str}")
    
    return {
        'process_instance_id': pid,
        'title': get_val('title'),
        'create_time': get_val(['create_time', 'createTime']),
        'finish_time': get_val(['finish_time', 'finishTime']),
        'originator_userid': originator_userid,
        'originator_dept_id': get_val(['originator_dept_id', 'originatorDeptId']),
        'status': get_val('status'),
        'result': get_val('result'),
        'business_id': get_val(['business_id', 'businessId']),
        'process_code': get_val(['process_code', 'processCode']),
        'form_component_values': form_values,
        'originator_name': originator_name,
        'current_approvers': current_approvers_str,
        'tasks': tasks # Now we process and save this to DB
    }

def sync_single_instance(process_instance_id):
    """Fetch and sync a single instance."""
    try:
        detail = dt_client.get_process_instance_detail(process_instance_id)
        if not detail:
            logger.warning(f"Could not fetch details for {process_instance_id}")
            return
        
        # Pass the known ID to ensure it exists in the record
        record = transform_process_instance(detail, forced_id=process_instance_id)
        
        # Temporary Debug: Print first few tasks or important fields
        inst_status = record.get('status')
        approvers = record.get('current_approvers')
        
        log_msg = f"Synced: {process_instance_id} | Status: {inst_status} | Approvers: {approvers} | Title: {record.get('title')}"
        logger.info(log_msg)
        
        upsert_process_instance(record)
    except Exception as e:
        logger.error(f"Failed to sync instance {process_instance_id}: {e}")

# --- User Sync ---

def sync_users():
    """
    Fetch all users from DingTalk and save to DB.
    """
    logger.info("Starting User Sync...")
    try:
        # 1. Get all departments
        logger.info("Fetching departments...")
        dept_ids = dt_client.get_department_list_ids()
        logger.info(f"Found {len(dept_ids)} departments.")

        # 2. Get users for each department
        all_users = []
        for i, dept_id in enumerate(dept_ids):
            users = dt_client.get_dept_users(dept_id)
            all_users.extend(users)
            if i % 10 == 0:
                logger.info(f"Processed {i+1}/{len(dept_ids)} departments...")
        
        # Deduplicate
        unique_users = {u['userid']: u for u in all_users}.values()
        user_list = list(unique_users)
        
        logger.info(f"Found {len(user_list)} unique users. Upserting to DB...")
        upsert_dingtalk_users(user_list)
        logger.info("User Sync Completed.")
        
    except Exception as e:
        logger.critical(f"Failed to sync users: {e}")

# --- Stream Mode Handlers ---

def on_bpms_instance_change(event):
    """
    Callback for bpms_instance_change event.
    """
    try:
        # data is usually in event.data (depending on SDK version, sometimes event.message)
        data = json.loads(event.data)
        logger.info(f"Received Event: {event.topic} -> {data}")
        
        process_instance_id = data.get('processInstanceId')
        
        if process_instance_id:
            logger.info(f"Processing Stream Event for Instance: {process_instance_id}")
            sync_single_instance(process_instance_id)
            
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Error handling event: {e}")
        return {"status": "error", "message": str(e)}

def start_stream_mode():
    logger.info("Starting DingTalk Stream Mode...")
    
    client_id = os.getenv('DINGTALK_CLIENT_ID')
    client_secret = os.getenv('DINGTALK_CLIENT_SECRET')
    
    if not client_id or not client_secret:
        logger.critical("DINGTALK_CLIENT_ID or DINGTALK_CLIENT_SECRET not set.")
        return

    credential = Credential(client_id, client_secret)
    client = DingTalkStreamClient(credential)
    
    # Register callback for approval instance changes
    client.register_callback_handler("bpms_instance_change", on_bpms_instance_change)
    
    logger.info("Stream Client Initialized. Listening for events...")
    client.start_forever()

# --- History Mode ---

def start_history_mode(start_date, end_date, process_code):
    logger.info(f"Starting History Mode: {start_date} to {end_date} for Process Code: {process_code}")
    
    # 1. Get IDs
    try:
        ids = dt_client.get_process_instance_ids(f"{start_date} 00:00:00", f"{end_date} 23:59:59", process_code)
        logger.info(f"Found {len(ids)} instances.")
    except Exception as e:
        logger.critical(f"Failed to fetch IDs: {e}")
        return

    # 2. Iterate and Sync
    total = len(ids)
    for i, pid in enumerate(ids):
        logger.info(f"Syncing {i+1}/{total}...")
        sync_single_instance(pid)
        # Avoid rate limits
        time.sleep(0.2)

    logger.info("History Sync Completed.")

def list_process_codes():
    """
    Helper to list process codes by fetching a user and listing their visible processes.
    """
    logger.info("Discovering Process Codes...")
    try:
        # 1. Get a department (root)
        dept_ids = dt_client.get_department_list_ids()
        if not dept_ids:
            logger.error("No departments found.")
            return

        # 2. Get a user from the first department
        users = dt_client.get_dept_users(dept_ids[0])
        if not users:
            logger.error("No users found in root department to query process list.")
            return
        
        test_user_id = users[0]['userid']
        logger.info(f"Using user {users[0]['name']} ({test_user_id}) to query template list...")
        
        # 3. Get process list
        process_list = dt_client.get_user_visible_process_codes(test_user_id)
        
        if not process_list:
            logger.warning("No accessible process codes found for this user.")
            return

        print("\n=== Available Process Codes ===")
        for p in process_list:
            print(f"Name: {p.get('name')}")
            print(f"Code: {p.get('process_code')}")
            print("-" * 30)
        print("===============================\n")
        
    except Exception as e:
        logger.error(f"Failed to list process codes: {e}")

def main():
    # Initialize DB
    create_table_if_not_exists()
    
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python main.py stream")
        print("  python main.py history <start_date> <end_date> [process_code]")
        print("  python main.py history (defaults to last month)")
        print("  python main.py list-codes  <-- Use to find your PROCESS_CODE")
        print("  python main.py sync-users  <-- Cache Users")
        return

    mode = sys.argv[1]
    
    if mode == 'stream':
        start_stream_mode()

    elif mode == 'list-codes':
        list_process_codes()

    elif mode == 'sync-users':
        sync_users()
        
    elif mode == 'history':
        process_code_env = os.getenv('PROCESS_CODE', '')
        
        # Parse process codes: split by comma, strip whitespace, remove comments (starting with #)
        env_codes = [p.strip() for p in process_code_env.split(',') if p.strip() and not p.strip().startswith('#')]

        if len(sys.argv) >= 4:
            start_date = sys.argv[2]
            end_date = sys.argv[3]
            # Priority: Arg > Env
            if len(sys.argv) >= 5:
                process_codes = [sys.argv[4]]
            else:
                process_codes = env_codes
        else:
            start_date, end_date = get_last_month_range()
            process_codes = env_codes
            
        if not process_codes:
            logger.critical("Process Code is required for history mode. Set PROCESS_CODE env var (comma separated) or pass as argument.")
            logger.info("Tip: Run 'python main.py list-codes' to see available codes.")
            return
        
        for p_code in process_codes:
            start_history_mode(start_date, end_date, p_code)
        
    else:
        logger.error(f"Unknown mode: {mode}")

if __name__ == "__main__":
    main()

