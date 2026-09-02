import sys
import asyncio
import logging
import os
import json
import time
import threading
from datetime import datetime, date, timedelta
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv

# Local modules
from db import (
    create_table_if_not_exists, 
    upsert_process_instance, 
    upsert_dingtalk_users, 
    get_user_name_from_db,
    get_instance_status,
    upsert_operation_records,
    update_instance_fingerprint,
    get_instance_ids_for_refresh,
)
from dingtalk_client import DingTalkClient
from asset_service import (
    enqueue_instance_assets,
    sync_assets,
    sync_assets_for_instance,
    status_report,
    count_pending_assets,
)
from minio_client import ensure_bucket

# DingTalk Stream SDK
from dingtalk_stream import DingTalkStreamClient, Credential, EventHandler, AckMessage

# ETL
from etl import parse_component_list

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
    
    # Run ETL
    form_values_cleaned = parse_component_list(form_values)

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
        'tasks': tasks, # Now we process and save this to DB
        'form_values_cleaned': form_values_cleaned
    }

def build_operation_records(instance_data):
    """
    从详情接口返回的 operation_records 构造操作记录/评论列表。

    钉钉没有公开的"评论列表"接口，评论以 operation_type='ADD_REMARK' 的形式
    混在 operation_records 里返回，因此这是获取评论的唯一途径。
    同一实例内用数组下标 seq 作为唯一键（同一秒可能出现多条记录）。
    """
    records = []
    for i, rec in enumerate(instance_data.get('operation_records') or []):
        uid = rec.get('userid')
        records.append({
            'seq': i,
            'operation_type': rec.get('operation_type'),
            'operation_result': rec.get('operation_result'),
            'userid': uid,
            'user_name': get_user_name_cached(uid),
            'remark': rec.get('remark') or None,
            'operation_time': rec.get('date'),
        })
    return records


# ---------------------------------------------------------------- 即时转储
# stream 事件回调必须尽快 ACK，转储（下载图片 + 调接口换附件链接）可能耗时数十秒，
# 放在回调里同步执行会让钉钉迟迟收不到 ACK 从而重推事件。因此登记完成后交给后台线程。
_transfer_lock = threading.Lock()
_transfer_inflight = set()


def _transfer_instance_async(process_instance_id):
    """在后台线程转储单个实例的资产；同一实例不会重复拉起线程。"""
    with _transfer_lock:
        if process_instance_id in _transfer_inflight:
            return False
        _transfer_inflight.add(process_instance_id)

    def _worker():
        try:
            sync_assets_for_instance(process_instance_id)
        except Exception as exc:
            logger.error(f"后台转储失败 {process_instance_id}: {exc}")
        finally:
            with _transfer_lock:
                _transfer_inflight.discard(process_instance_id)

    threading.Thread(
        target=_worker,
        name=f"asset-{str(process_instance_id)[:16]}",
        daemon=True,
    ).start()
    return True


def sync_single_instance(process_instance_id, force=False, auto_transfer=False):
    """
    Fetch and sync a single instance, including operation records (comments)
    and asset registration.

    force=True 时忽略终态跳过。评论是在审批完成后才可能追加的，
    所以增量刷新必须传 force=True，否则永远拿不到新评论。

    auto_transfer=True 时，登记完新资产立刻在后台线程转储到 MinIO（stream 模式用）。
    为 False 时只登记不下载，积压的资产由 `python main.py assets` 或
    `python asset_backfill.py transfer` 批量处理。
    """
    try:
        # Idempotency Check
        # If instance exists and is already in a final state, skip sync.
        # Final states: COMPLETED, TERMINATED
        if not force:
            existing_status = get_instance_status(process_instance_id)
            if existing_status in ['COMPLETED', 'TERMINATED']:
                logger.info(f"Skipping {process_instance_id} (Already {existing_status})")
                return

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

        # 操作记录与评论（ADD_REMARK）
        records = build_operation_records(detail)
        if records:
            upsert_operation_records(process_instance_id, records)
            update_instance_fingerprint(
                process_instance_id,
                op_record_count=len(records),
                last_op_time=records[-1].get('operation_time'),
            )
            comments = sum(1 for r in records if r['operation_type'] == 'ADD_REMARK')
            if comments:
                logger.info(f"  -> {len(records)} 条操作记录（含 {comments} 条评论）")

        # 登记图片/附件资产（只写库，不下载）
        inserted = enqueue_instance_assets(
            process_instance_id,
            detail.get('form_component_values'),
            operation_records=records
        )
        if inserted:
            logger.info(f"  -> 新登记 {inserted} 个待转储资产")
            if auto_transfer:
                if _transfer_instance_async(process_instance_id):
                    logger.info(f"  -> 已在后台线程启动即时转储")
    except Exception as e:
        logger.error(f"Failed to sync instance {process_instance_id}: {e}")


def _report_pending_assets():
    """history / refresh 结束后提示还有多少资产没转储。"""
    try:
        pending = count_pending_assets()
    except Exception as exc:
        logger.warning(f"统计待转储资产失败: {exc}")
        return
    if pending:
        logger.info(
            f"还有 {pending} 个资产待转储，"
            f"执行 `python main.py assets` 或 `python asset_backfill.py transfer` 处理")

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

class AllEventHandler(EventHandler):
    """
    Catch-all event handler to log all incoming events for debugging and processing.
    This is the correct way to handle event subscriptions in DingTalk Stream mode.
    """
    async def process(self, event):
        """
        Log all events that come through the stream and process BPMS events.
        Event types are determined from event.headers (dict-like) containing 'eventType'.
        """
        # Extract event properties - different SDK versions may have different structures
        headers = getattr(event, 'headers', {})
        data = getattr(event, 'data', '{}')
        
        # headers might be a dict or an object with attributes
        if isinstance(headers, dict):
            event_type = headers.get('eventType') or headers.get('event_type', 'unknown')
            topic = headers.get('topic', 'unknown')
        else:
            event_type = getattr(headers, 'eventType', None) or getattr(headers, 'event_type', 'unknown')
            topic = getattr(headers, 'topic', 'unknown')
        
        logger.info(f"[AllEventHandler] *** EVENT RECEIVED ***")
        logger.info(f"  EventType: {event_type}")
        logger.info(f"  Topic: {topic}")
        logger.info(f"  Headers: {headers}")
        logger.info(f"  Data (first 500 chars): {str(data)[:500]}")
        
        # Process BPMS events (approval workflow events)
        if event_type in ['bpms_instance_change', 'bpms_task_change'] or 'bpms' in str(event_type).lower():
            try:
                if isinstance(data, str):
                    parsed_data = json.loads(data)
                else:
                    parsed_data = data
                process_instance_id = parsed_data.get('processInstanceId')
                if process_instance_id:
                    logger.info(f"  -> Processing BPMS event, syncing instance: {process_instance_id}")
                    # Run sync in executor to not block the async loop
                    # force=True：终态实例也可能被追加评论，必须重新拉取
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(
                        None, lambda: sync_single_instance(
                            process_instance_id, force=True, auto_transfer=True))
            except Exception as e:
                logger.error(f"  -> Error processing BPMS event: {e}")
        
        return AckMessage.STATUS_OK, 'OK'


def start_stream_mode():
    logger.info("Starting DingTalk Stream Mode...")
    
    client_id = os.getenv('DINGTALK_CLIENT_ID')
    client_secret = os.getenv('DINGTALK_CLIENT_SECRET')
    
    if not client_id or not client_secret:
        logger.critical("DINGTALK_CLIENT_ID or DINGTALK_CLIENT_SECRET not set.")
        return

    credential = Credential(client_id, client_secret)
    client = DingTalkStreamClient(credential)
    
    # For event subscriptions (审批事件), use register_all_event_handler
    # The event type is determined from headers.event_type in the handler
    # NOTE: register_callback_handler is for chatbot callbacks, NOT for events
    client.register_all_event_handler(AllEventHandler())

    
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

# --- Refresh Mode (评论增量刷新) ---

def start_refresh_mode(since_days=None, limit=None):
    """
    重新拉取已有实例的详情，刷新操作记录与评论。

    钉钉没有"评论列表"接口，也不保证评论变更会推送事件，因此评论只能靠轮询。
    终态实例（COMPLETED/TERMINATED）同样会被处理，因为评论通常在审批结束后才追加。
    """
    logger.info(f"Starting Refresh Mode: since_days={since_days}, limit={limit}")
    ids = get_instance_ids_for_refresh(limit=limit, since_days=since_days)
    logger.info(f"Found {len(ids)} instances to refresh.")

    total = len(ids)
    for i, pid in enumerate(ids):
        logger.info(f"Refreshing {i+1}/{total}...")
        sync_single_instance(pid, force=True)
        time.sleep(0.2)

    _report_pending_assets()
    logger.info("Refresh Completed.")

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
        print("  python main.py refresh [days] [limit]   <-- 增量刷新操作记录与评论")
        print("  python main.py assets [limit] [image|attachment]  <-- 转储图片/附件到 MinIO")
        print("  python main.py asset-status             <-- 查看转储进度")
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

    elif mode == 'refresh':
        # python main.py refresh [days] [limit]
        since_days = None
        limit = None
        for arg in sys.argv[2:4]:
            if arg.isdigit():
                if since_days is None:
                    since_days = int(arg)
                else:
                    limit = int(arg)
        start_refresh_mode(since_days=since_days, limit=limit)

    elif mode == 'assets':
        # python main.py assets [limit] [image|attachment]
        limit = 200
        atype = None
        for arg in sys.argv[2:4]:
            if arg.isdigit():
                limit = int(arg)
            elif arg in ('image', 'attachment'):
                atype = arg
        ensure_bucket()
        stats = sync_assets(limit=limit, asset_type=atype)
        print(stats)
        status_report()

    elif mode == 'asset-status':
        status_report()
        
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

