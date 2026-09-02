import io
import os
import logging
from datetime import timedelta, datetime

from dotenv import load_dotenv
from minio import Minio
from minio.error import S3Error

load_dotenv()
logger = logging.getLogger(__name__)

# 对象键前缀：内容寻址，结构 dingtalk/{type}/{yyyy}/{mm}/{hash前2位}/{hash}.{ext}
OBJECT_PREFIX = os.getenv('MINIO_OBJECT_PREFIX', 'dingtalk')

_client = None


def get_minio_client():
    """Return a cached MinIO client built from .env settings."""
    global _client
    if _client is not None:
        return _client

    endpoint = (os.getenv('MINIO_ENDPOINT') or '').strip()
    access_key = (os.getenv('MINIO_ACCESS_KEY') or '').strip()
    secret_key = (os.getenv('MINIO_SECRET_KEY') or '').strip()

    if not endpoint or not access_key or not secret_key:
        raise RuntimeError(
            "MinIO 未配置：请在 .env 中设置 MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY")

    secure = (os.getenv('MINIO_SECURE', 'false') or 'false').strip().lower() in ('1', 'true', 'yes')
    _client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
    return _client


def get_bucket():
    bucket = (os.getenv('MINIO_DEFAULT_BUCKET') or '').strip()
    if not bucket:
        raise RuntimeError("MinIO 未配置：请在 .env 中设置 MINIO_DEFAULT_BUCKET")
    return bucket


def ensure_bucket():
    """
    Make sure the target bucket exists.
    The bucket is intentionally left WITHOUT a public policy — access is granted
    only through backend-generated presigned URLs.
    """
    client = get_minio_client()
    bucket = get_bucket()
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
        logger.info(f"Created bucket `{bucket}`")
    return bucket


def build_object_key(asset_type, sha256, ext, when=None):
    """
    Build a content-addressed object key.
    Example: dingtalk/image/2026/09/a3/a3f5c1d8...9f.jpg

    The sha256 prefix keeps directory fan-out low; identical content always maps
    to the same key, which makes re-runs idempotent.
    """
    when = when or datetime.now()
    ext = (ext or '').lstrip('.').lower()
    return f"{OBJECT_PREFIX}/{asset_type}/{when:%Y/%m}/{sha256[:2]}/{sha256}.{ext}"


def object_exists(object_key):
    """Return the stored object size, or None when the object is absent."""
    client = get_minio_client()
    bucket = get_bucket()
    try:
        stat = client.stat_object(bucket, object_key)
        return stat.size
    except S3Error as e:
        if e.code in ('NoSuchKey', 'NoSuchObject', 'NotFound'):
            return None
        raise


def put_bytes(object_key, data, content_type='application/octet-stream'):
    """
    Upload raw bytes. Returns the number of bytes written.
    Existing objects are overwritten only when the content differs — callers are
    expected to check object_exists() first.
    """
    client = get_minio_client()
    bucket = get_bucket()
    length = len(data)
    client.put_object(
        bucket, object_key,
        io.BytesIO(data), length,
        content_type=content_type,
    )
    return length


_public_client_cache = None


def _public_client():
    """
    Client used only for signing.

    Cached because constructing a Minio client costs a bucket-location round
    trip; without caching every signed URL would pay for one.

    MINIO_ENDPOINT is often 127.0.0.1 (this script runs on the MinIO host), but a
    URL containing 127.0.0.1 is unusable by any other machine — the browser would
    resolve it to itself. Set MINIO_PUBLIC_ENDPOINT (LAN IP / domain reachable by
    the browser) so signed URLs point somewhere useful while uploads still go
    through the local endpoint. Left unset, behaviour is unchanged.
    """
    global _public_client_cache

    public_endpoint = (os.getenv('MINIO_PUBLIC_ENDPOINT') or '').strip()
    if not public_endpoint or public_endpoint == (os.getenv('MINIO_ENDPOINT') or '').strip():
        return get_minio_client()
    if _public_client_cache is not None:
        return _public_client_cache

    # MinIO SDK wants host:port, not a URL. Accept http(s):// too and strip it.
    secure = (os.getenv('MINIO_SECURE', 'false') or 'false').strip().lower() \
        in ('1', 'true', 'yes')
    low = public_endpoint.lower()
    if low.startswith('https://'):
        public_endpoint, secure = public_endpoint[8:], True
    elif low.startswith('http://'):
        public_endpoint, secure = public_endpoint[7:], False
    public_endpoint = public_endpoint.rstrip('/')

    _public_client_cache = Minio(
        public_endpoint,
        access_key=(os.getenv('MINIO_ACCESS_KEY') or '').strip(),
        secret_key=(os.getenv('MINIO_SECRET_KEY') or '').strip(),
        secure=secure,
    )
    return _public_client_cache


def presigned_get_url(object_key, expires_seconds=None, filename=None):
    """
    Generate a temporary, signed download URL.
    Images default to 1 hour, files to 5 minutes.

    Signature is computed by _public_client() so the host in the URL is
    MINIO_PUBLIC_ENDPOINT when configured; otherwise identical to before.
    """
    client = _public_client()
    bucket = get_bucket()
    if expires_seconds is None:
        expires_seconds = int(os.getenv('MINIO_PRESIGN_EXPIRES', 3600))
    extra = None
    if filename:
        safe = filename.replace('"', '')
        extra = {'response-content-disposition': f'attachment; filename="{safe}"'}
    return client.presigned_get_object(
        bucket, object_key,
        expires=timedelta(seconds=expires_seconds),
        response_headers=extra,
    )


def get_bytes(object_key):
    """Download an object back as bytes (used by integrity checks)."""
    client = get_minio_client()
    resp = client.get_object(get_bucket(), object_key)
    try:
        return resp.read()
    finally:
        resp.close()
        resp.release_conn()


def health_check():
    """Verify connectivity and bucket availability. Returns a short status dict."""
    client = get_minio_client()
    bucket = get_bucket()
    exists = client.bucket_exists(bucket)
    return {'endpoint': os.getenv('MINIO_ENDPOINT'), 'bucket': bucket, 'bucket_exists': exists}


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    info = health_check()
    print(f"Endpoint : {info['endpoint']}")
    print(f"Bucket   : {info['bucket']} (exists={info['bucket_exists']})")

    # Round-trip self test with a tiny throwaway object
    ensure_bucket()
    import hashlib
    payload = b'workbuddy minio self-test'
    digest = hashlib.sha256(payload).hexdigest()
    key = build_object_key('image', digest, 'txt')
    put_bytes(key, payload, 'text/plain')
    print(f"Uploaded : {key}")
    print(f"Exists   : {object_exists(key)} bytes")
    print(f"Signed   : {presigned_get_url(key, 300)[:110]}...")
    print(f"Fetched  : {get_bytes(key)!r}")
