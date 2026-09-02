# -*- coding: utf-8 -*-
"""
审批图片与附件的登记、下载与转储服务。

两条链路：
  image      — 直接 GET 钉钉 static.dingtalk.com 直链
  attachment — 先调 API 把 fileId 换成 download_uri（仅 15 分钟有效），立即下载

设计规范：
  * object key 由内容 sha256 生成，重复执行天然幂等
  * 原表字段一律不改动，只在 process_asset 中记录转储结果
  * 转储失败不改状态为终态，只累加 retry_count，留待下轮重试
"""
import os
import io
import json
import re
import time
import hashlib
import logging
from datetime import datetime

import requests
from dotenv import load_dotenv

from db import (
    insert_assets_pending,
    fetch_pending_assets,
    mark_asset_result,
    count_assets_by_status,
    count_retryable_assets,
    update_instance_fingerprint,
)
from minio_client import (
    get_bucket,
    ensure_bucket,
    build_object_key,
    object_exists,
    put_bytes,
)

load_dotenv()
logger = logging.getLogger(__name__)

PHOTO_COMPONENT = 'DDPhotoField'
ATTACH_COMPONENT = 'DDAttachment'

MAX_IMAGE_BYTES = int(os.getenv('MAX_IMAGE_BYTES', 25 * 1024 * 1024))
MAX_FILE_BYTES = int(os.getenv('MAX_FILE_BYTES', 100 * 1024 * 1024))
API_SLEEP = float(os.getenv('ASSET_API_SLEEP', 0.3))


# ---------------------------------------------------------------- HTTP helpers

def _request(method, url, **kwargs):
    """
    发起 HTTP 请求。默认遵循系统代理设置，遇到代理故障自动降级为直连重试。
    """
    session = requests.Session()
    try:
        return session.request(method, url, **kwargs)
    except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError) as exc:
        logger.warning(f"代理连接失败，改用直连重试: {type(exc).__name__}")
        direct = requests.Session()
        direct.trust_env = False
        return direct.request(method, url, **kwargs)


def _sniff_image_mime(data):
    """按 magic bytes 判定图片真实类型，不信任 URL 后缀。"""
    if data[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if data[:2] == b'BM':
        return 'image/bmp'
    return None


# ------------------------------------------------------------ 表单资产登记

def _iter_components(node):
    """
    递归遍历表单结构并产出每个组件字典。
    DDBizSuite / TableField 的值是以 JSON 字符串内嵌的，必须解析后继续下钻，
    否则会漏掉一半以上的图片。
    """
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_components(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_components(value)
    elif isinstance(node, str):
        text = node.strip()
        if text[:1] in '[{':
            try:
                yield from _iter_components(json.loads(text))
            except Exception:
                pass


def _as_list(value):
    """把组件的 value 规整成列表：JSON 字符串 / 原生列表 / 单值。"""
    if not value:
        return []
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in '[{':
            try:
                parsed = json.loads(text)
                return parsed if isinstance(parsed, list) else [parsed]
            except Exception:
                return []
        return [text]
    if isinstance(value, list):
        return value
    return [value]


def _extract_form_assets(form_values):
    """从表单组件中提取待转储资产。"""
    if isinstance(form_values, str):
        try:
            form_values = json.loads(form_values)
        except Exception:
            return []

    assets = []
    seen = set()

    for comp in _iter_components(form_values):
        ctype = comp.get('component_type') or comp.get('componentType')
        cname = comp.get('name') or (comp.get('props') or {}).get('label')
        value = comp.get('value')

        if ctype == PHOTO_COMPONENT:
            for item in _as_list(value):
                if not isinstance(item, str) or not item.startswith('http'):
                    continue
                key = ('image', item)
                if key in seen:
                    continue
                seen.add(key)
                assets.append({
                    'asset_type': 'image',
                    'process_instance_id': None,   # 由调用方填充
                    'asset_ref': item,
                    'component_name': cname,
                    'file_name': None,
                    'source_url': item,
                    'file_id': None,
                    'space_id': None,
                    'declared_size': None,
                })

        elif ctype == ATTACH_COMPONENT:
            for item in _as_list(value):
                if not isinstance(item, dict):
                    continue
                file_id = item.get('fileId') or item.get('file_id')
                if not file_id:
                    continue
                key = ('attachment', str(file_id))
                if key in seen:
                    continue
                seen.add(key)
                assets.append({
                    'asset_type': 'attachment',
                    'process_instance_id': None,
                    'asset_ref': str(file_id),
                    'component_name': cname,
                    'file_name': item.get('fileName'),
                    'source_url': None,
                    'file_id': str(file_id),
                    'space_id': str(item.get('spaceId') or ''),
                    'declared_size': item.get('fileSize'),
                })

    return assets


REMARK_IMAGE_PATTERN = re.compile(
    r'https?://[^\s"\'<>)}\]]+(?:\.(?:jpg|jpeg|png|gif|webp|bmp)|\/media\/|\/ddmedia\/|\/yundisk)[^\s"\'<>)}\]]*',
    re.IGNORECASE
)


def extract_operation_assets(operation_records):
    """
    从审批操作记录与评论中提取图片与附件资产。
    """
    if not operation_records:
        return []

    if isinstance(operation_records, str):
        try:
            operation_records = json.loads(operation_records)
        except Exception:
            return []

    assets = []
    seen = set()

    for op in operation_records:
        if not isinstance(op, dict):
            continue

        user_label = op.get('user_name') or op.get('userid') or '用户'
        op_type = op.get('operation_type') or 'REMARK'
        cname = f"评论图片 ({user_label})" if op_type == 'ADD_REMARK' else f"审批备注图片 ({user_label})"

        # 1. 从 remark 文本提取图片/媒体 URL
        remark = op.get('remark')
        if remark and isinstance(remark, str):
            for u in REMARK_IMAGE_PATTERN.findall(remark):
                u = u.rstrip(',.;)]}')
                key = ('image', u)
                if key in seen:
                    continue
                seen.add(key)
                assets.append({
                    'asset_type': 'image',
                    'process_instance_id': None,
                    'asset_ref': u,
                    'component_name': cname,
                    'file_name': None,
                    'source_url': u,
                    'file_id': None,
                    'space_id': None,
                    'declared_size': None,
                })

        # 2. 检查是否有结构化 attachments / photos
        for field in ('attachments', 'photos', 'images', 'files'):
            val = op.get(field)
            if not val:
                continue
            for item in _as_list(val):
                if isinstance(item, str) and item.startswith('http'):
                    key = ('image', item)
                    if key not in seen:
                        seen.add(key)
                        assets.append({
                            'asset_type': 'image',
                            'process_instance_id': None,
                            'asset_ref': item,
                            'component_name': cname,
                            'file_name': None,
                            'source_url': item,
                            'file_id': None,
                            'space_id': None,
                            'declared_size': None,
                        })
                elif isinstance(item, dict):
                    file_id = item.get('fileId') or item.get('file_id')
                    url = item.get('url') or item.get('downloadUrl')
                    if file_id:
                        key = ('attachment', str(file_id))
                        if key not in seen:
                            seen.add(key)
                            assets.append({
                                'asset_type': 'attachment',
                                'process_instance_id': None,
                                'asset_ref': str(file_id),
                                'component_name': f"评论附件 ({user_label})",
                                'file_name': item.get('fileName'),
                                'source_url': url,
                                'file_id': str(file_id),
                                'space_id': str(item.get('spaceId') or ''),
                                'declared_size': item.get('fileSize'),
                            })
                    elif url and url.startswith('http'):
                        key = ('image', url)
                        if key not in seen:
                            seen.add(key)
                            assets.append({
                                'asset_type': 'image',
                                'process_instance_id': None,
                                'asset_ref': url,
                                'component_name': cname,
                                'file_name': item.get('fileName'),
                                'source_url': url,
                                'file_id': None,
                                'space_id': None,
                                'declared_size': None,
                            })

    return assets


def extract_assets(form_values=None, operation_records=None):
    """
    从表单组件及操作记录（评论）中提取待转储资产（不去重到全局，只在同一实例内去重）。
    """
    assets = []
    seen = set()

    if form_values:
        for a in _extract_form_assets(form_values):
            key = (a['asset_type'], a['asset_ref'])
            if key not in seen:
                seen.add(key)
                assets.append(a)

    if operation_records:
        for a in extract_operation_assets(operation_records):
            key = (a['asset_type'], a['asset_ref'])
            if key not in seen:
                seen.add(key)
                assets.append(a)

    return assets


def enqueue_instance_assets(process_instance_id, form_values=None, operation_records=None):
    """
    登记单个实例的资产（包含表单组件及评论中的图片/附件，只写库，不做网络请求）。
    """
    if isinstance(form_values, str):
        try:
            form_values = json.loads(form_values)
        except Exception:
            form_values = None

    assets = extract_assets(form_values, operation_records)
    for a in assets:
        a['process_instance_id'] = process_instance_id

    inserted = insert_assets_pending(assets) if assets else 0
    update_instance_fingerprint(process_instance_id, None, None, asset_synced=1)
    return inserted


# ---------------------------------------------------------------- 下载

def download_image(url):
    """下载钉钉图片直链，返回 (bytes, content_type)。"""
    resp = _request('GET', url, timeout=(10, 60), stream=True)
    resp.raise_for_status()

    chunks, total = [], 0
    for chunk in resp.iter_content(65536):
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_IMAGE_BYTES:
            raise ValueError(f"图片超过大小上限 {MAX_IMAGE_BYTES} 字节")
        chunks.append(chunk)

    data = b''.join(chunks)
    ctype = (resp.headers.get('Content-Type') or '').split(';')[0].strip()
    if not ctype.startswith('image/'):
        ctype = _sniff_image_mime(data) or 'application/octet-stream'
    if ctype == 'application/octet-stream':
        raise ValueError("下载内容不是可识别的图片格式")
    return data, ctype


def download_attachment(process_instance_id, file_id):
    """
    下载审批附件。必须"换链接后立即下载"——download_uri 只有 15 分钟有效期，
    批量预取链接再排队下载会导致链接过期。
    """
    from dingtalk_client import DingTalkClient   # 延迟导入，避免循环依赖

    client = DingTalkClient()
    uri, space_id = client.get_attachment_download_url(process_instance_id, file_id)
    if not uri:
        raise RuntimeError("钉钉未返回下载链接（可能无权限或文件已删除）")

    resp = _request('GET', uri, timeout=(10, 120))
    resp.raise_for_status()
    data = resp.content
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"附件超过大小上限 {MAX_FILE_BYTES} 字节")
    return data, 'application/octet-stream'


# ---------------------------------------------------------------- 转储

def _ext_from(asset, content_type):
    """推断扩展名：附件用原文件名，图片用 MIME。"""
    if asset['asset_type'] == 'attachment' and asset.get('file_name'):
        name = asset['file_name']
        if '.' in name:
            return name.rsplit('.', 1)[1].lower()[:8]
    if asset['asset_type'] == 'image' and asset.get('source_url'):
        tail = asset['source_url'].split('?')[0]
        if '.' in tail:
            return tail.rsplit('.', 1)[1].lower()[:8]
    mapping = {
        'image/jpeg': 'jpg', 'image/png': 'png', 'image/gif': 'gif',
        'image/webp': 'webp', 'image/bmp': 'bmp',
    }
    return mapping.get((content_type or '').lower(), 'bin')


def transfer_asset(asset):
    """
    转储单条资产。成功返回 'SUCCESS'，命中去重返回 'SKIPPED'，失败抛出。
    """
    asset_id = asset['id']

    # 1. 取内容
    if asset['asset_type'] == 'image':
        data, content_type = download_image(asset['source_url'])
    else:
        data, content_type = download_attachment(asset['process_instance_id'], asset['file_id'])

    # 2. 完整性校验（附件以钉钉声明大小为准）
    declared = asset.get('declared_size')
    if declared and int(declared) != len(data):
        raise ValueError(f"大小不一致：钉钉声明 {declared}，实得 {len(data)}")

    # 3. 内容寻址
    sha256 = hashlib.sha256(data).hexdigest()
    object_key = build_object_key(asset['asset_type'], sha256, _ext_from(asset, content_type))
    bucket = get_bucket()

    # 4. 去重：同内容已存在则直接复用
    existing = object_exists(object_key)
    if existing is not None and existing == len(data):
        mark_asset_result(asset_id, 'SKIPPED', bucket=bucket, object_key=object_key,
                          sha256=sha256, size_bytes=len(data), content_type=content_type,
                          error_message='同内容已存在，复用')
        return 'SKIPPED'

    # 5. 上传
    put_bytes(object_key, data, content_type)
    mark_asset_result(asset_id, 'SUCCESS', bucket=bucket, object_key=object_key,
                      sha256=sha256, size_bytes=len(data), content_type=content_type)
    return 'SUCCESS'


def sync_assets(limit=200, asset_type=None, sleep=API_SLEEP):
    """
    批量转储待处理资产。返回统计字典。
    """
    ensure_bucket()
    pending = fetch_pending_assets(asset_type=asset_type, limit=limit)
    if not pending:
        return {'fetched': 0, 'success': 0, 'skipped': 0, 'failed': 0}

    stats = {'fetched': len(pending), 'success': 0, 'skipped': 0, 'failed': 0}
    for i, asset in enumerate(pending, 1):
        label = asset.get('file_name') or (asset.get('source_url') or '')[-48:]
        try:
            result = transfer_asset(asset)
            stats['success' if result == 'SUCCESS' else 'skipped'] += 1
            logger.info(f"[{i}/{len(pending)}] {result} {asset['asset_type']} {label}")
        except Exception as exc:
            stats['failed'] += 1
            logger.error(f"[{i}/{len(pending)}] FAILED {asset['asset_type']} {label}: {exc}")
            mark_asset_result(asset['id'], 'FAILED', error_message=str(exc)[:500])
        time.sleep(sleep)

    logger.info(
        f"本轮结束：成功 {stats['success']}，复用 {stats['skipped']}，失败 {stats['failed']}")
    return stats


def sync_assets_for_instance(process_instance_id, limit=200, sleep=API_SLEEP):
    """
    转储单个实例的待处理资产 —— 供 stream 模式做「新审批即时转储」。

    与 sync_assets 的区别：只处理这一个实例，避免在事件回调里把全库积压任务
    全部拉进来跑。只捞 PENDING/FAILED 且 retry_count 未超限的行。
    """
    ensure_bucket()
    pending = fetch_pending_assets(
        limit=limit, process_instance_id=process_instance_id)
    if not pending:
        return {'fetched': 0, 'success': 0, 'skipped': 0, 'failed': 0}

    stats = {'fetched': len(pending), 'success': 0, 'skipped': 0, 'failed': 0}
    for asset in pending:
        label = asset.get('file_name') or (asset.get('source_url') or '')[-48:]
        try:
            result = transfer_asset(asset)
            stats['success' if result == 'SUCCESS' else 'skipped'] += 1
            logger.info(
                f"[即时转储] {result} {asset['asset_type']} {label}")
        except Exception as exc:
            stats['failed'] += 1
            logger.error(
                f"[即时转储] FAILED {asset['asset_type']} {label}: {exc}")
            mark_asset_result(asset['id'], 'FAILED', error_message=str(exc)[:500])
        time.sleep(sleep)

    logger.info(
        f"实例 {process_instance_id} 转储完成：成功 {stats['success']}，"
        f"复用 {stats['skipped']}，失败 {stats['failed']}")
    return stats


# ---------------------------------------------------------------- 网站取数

# 图片与附件使用不同的签名有效期：图片在页面上停留时间长，附件基本是点了就下
PRESIGN_IMAGE = int(os.getenv('MINIO_PRESIGN_EXPIRES_IMAGE', 3600))
PRESIGN_FILE = int(os.getenv('MINIO_PRESIGN_EXPIRES_FILE', 300))


def get_instance_assets(process_instance_id, expires=None):
    """
    给网站用的资产列表。

    返回元素：
      {
        'asset_type': 'image' | 'attachment',
        'component_name': '图片',
        'file_name': '原始文件名',
        'url':  MinIO 签名 URL（转储成功时才有）
        'fallback_url': 钉钉原直链（图片有，附件为 None）
        'status': 'SUCCESS' | 'PENDING' | 'FAILED' | ...
      }

    网站应优先用 url，取不到时再回落到 fallback_url。
    """
    from db import fetch_assets_by_instance
    from minio_client import presigned_get_url

    rows = fetch_assets_by_instance(process_instance_id)
    result = []
    for row in rows:
        url = None
        if row.get('object_key') and row['status'] in ('SUCCESS', 'SKIPPED'):
            ttl = expires or (PRESIGN_IMAGE if row['asset_type'] == 'image' else PRESIGN_FILE)
            try:
                url = presigned_get_url(row['object_key'], ttl, filename=row.get('file_name'))
            except Exception as e:
                logger.error(f"生成签名 URL 失败 {row['object_key']}: {e}")

        result.append({
            'id': row['id'],
            'asset_type': row['asset_type'],
            'component_name': row.get('component_name'),
            'file_name': row.get('file_name'),
            'content_type': row.get('content_type'),
            'size_bytes': row.get('size_bytes'),
            'sha256': row.get('sha256'),
            'url': url,
            'fallback_url': row.get('source_url'),
            'status': row['status'],
        })
    return result


def build_url_map(process_instance_id, expires=None):
    """
    给网站用的「原直链 -> MinIO 签名 URL」映射表。

    适用于前端已经在渲染 form_values_cleaned 的场景：拿到这张表后按原 URL 做
    一次字符串替换即可切到 MinIO，无需改动渲染逻辑；映射里没有的 URL 保持原样，
    天然实现回退。
    """
    mapping = {}
    for item in get_instance_assets(process_instance_id, expires=expires):
        if item['url'] and item['fallback_url']:
            mapping[item['fallback_url']] = item['url']
    return mapping


def count_pending_assets():
    """
    还需处理的资产条数（PENDING 或 FAILED 且未超出重试上限）。
    口径与 fetch_pending_assets 完全一致（见 db.count_retryable_assets）。
    """
    return count_retryable_assets()


def status_report():
    """打印资产转储总览。"""
    counts = count_assets_by_status()
    total = sum(counts.values())
    print(f"\n=== 资产转储总览（共 {total} 条）===")
    for status in ('SUCCESS', 'SKIPPED', 'PENDING', 'FAILED'):
        if status in counts:
            print(f"  {status:<8} {counts[status]}")
    print()
    return counts


if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(levelname)s - %(message)s')
    args = [a for a in sys.argv[1:]]
    limit = 200
    atype = None
    if args:
        try:
            limit = int(args[0])
        except ValueError:
            atype = args[0]
    if len(args) > 1:
        try:
            limit = int(args[1])
        except ValueError:
            pass
    print(sync_assets(limit=limit, asset_type=atype))
    status_report()
