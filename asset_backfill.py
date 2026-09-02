# -*- coding: utf-8 -*-
"""
全量回填脚本：把历史审批单的图片与附件登记并转储到 MinIO。

用法：
    python asset_backfill.py register          # 只登记（扫本地库，零 API 调用）
    python asset_backfill.py transfer [limit]  # 只转储
    python asset_backfill.py all [limit]       # 登记 + 转储（默认）
    python asset_backfill.py verify [n]        # 抽样校验 MinIO 上的对象

说明：
    登记阶段完全走本地库，因为 form_component_values 已经在 process_instance 里，
    不需要再调钉钉接口。只有评论（operation_records）需要走 main.py refresh。
"""
import sys
import json
import random
import hashlib
import logging

from db import (get_connection, get_records_without_assets, insert_assets_pending,
                update_instance_fingerprint, fetch_pending_assets,
                count_assets_by_status)
from asset_service import extract_assets, enqueue_instance_assets, sync_assets, status_report
from minio_client import ensure_bucket, get_bytes

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BATCH = 500


def register_all():
    """扫描本地库登记所有未登记的资产，不发起任何网络请求。"""
    conn = get_connection()
    total_inserted = 0
    total_scanned = 0

    try:
        while True:
            ids = get_records_without_assets(limit=BATCH)
            if not ids:
                break

            with conn.cursor() as cursor:
                fmt = ','.join(['%s'] * len(ids))
                cursor.execute(
                    f"SELECT process_instance_id, form_component_values "
                    f"FROM process_instance WHERE process_instance_id IN ({fmt})", ids)
                rows = cursor.fetchall()

            for row in rows:
                pid = row['process_instance_id']
                raw = row['form_component_values']
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except Exception:
                        raw = None

                assets = extract_assets(raw)
                for a in assets:
                    a['process_instance_id'] = pid
                if assets:
                    total_inserted += insert_assets_pending(assets)

                update_instance_fingerprint(pid, asset_synced=1)
                total_scanned += 1

            logger.info(f"已扫描 {total_scanned} 条，累计新登记 {total_inserted} 个资产")
    finally:
        conn.close()

    logger.info(f"登记完成：扫描 {total_scanned} 条实例，新登记 {total_inserted} 个资产")
    return total_scanned, total_inserted


def register_operation_records():
    """扫描本地 process_operation_record 登记评论中的图片/附件。"""
    conn = get_connection()
    total_inserted = 0
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT process_instance_id, operation_type, userid, user_name, remark "
                "FROM process_operation_record WHERE remark IS NOT NULL AND remark != ''"
            )
            records = cursor.fetchall()

        logger.info(f"扫描到 {len(records)} 条本地操作与评论记录...")
        from collections import defaultdict
        grouped = defaultdict(list)
        for r in records:
            grouped[r['process_instance_id']].append(r)

        for pid, op_list in grouped.items():
            inserted = enqueue_instance_assets(pid, form_values=None, operation_records=op_list)
            total_inserted += inserted

        logger.info(f"评论资产扫描登记完成：新登记 {total_inserted} 个待转储项")
    except Exception as e:
        logger.error(f"评论资产登记异常: {e}")
    finally:
        conn.close()
    return total_inserted


def verify_sample(n=20):
    """从 MinIO 随机抽 n 个已转储对象，回读并校验 sha256 与大小。"""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, asset_type, object_key, sha256, size_bytes, file_name "
                "FROM process_asset WHERE status IN ('SUCCESS','SKIPPED') "
                "AND object_key IS NOT NULL")
            rows = cursor.fetchall()
    finally:
        conn.close()

    if not rows:
        print("没有已转储的资产可供校验")
        return 0, 0

    sample = random.sample(rows, min(n, len(rows)))
    ok = 0
    for r in sample:
        try:
            data = get_bytes(r['object_key'])
            digest = hashlib.sha256(data).hexdigest()
            if digest == r['sha256'] and len(data) == r['size_bytes']:
                ok += 1
                print(f"  OK   {r['asset_type']:<10} {r['size_bytes']:>8} B  {r['file_name'] or r['object_key'][-40:]}")
            else:
                print(f"  FAIL {r['object_key']} 哈希或大小不匹配")
        except Exception as e:
            print(f"  FAIL {r['object_key']}: {e}")

    print(f"\n校验通过 {ok}/{len(sample)}")
    return ok, len(sample)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'all'

    if mode == 'register':
        register_all()
        register_operation_records()
        status_report()

    elif mode == 'transfer':
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 200
        ensure_bucket()
        print(sync_assets(limit=limit))
        status_report()

    elif mode == 'verify':
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        verify_sample(n)

    elif mode == 'all':
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 200
        register_all()
        register_operation_records()
        ensure_bucket()
        print(sync_assets(limit=limit))
        status_report()
        print(f"待转储仍有 {count_assets_by_status().get('PENDING', 0)} 条，"
              f"可重复执行本命令继续。")

    else:
        print(__doc__)


if __name__ == '__main__':
    main()
