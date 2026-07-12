# SPDX-License-Identifier: AGPL-3.0-or-later
"""Uploads attachment files to Fluxer's S3/SeaweedFS CDN bucket.

Object key scheme mirrors makeAttachmentCdnKey():
    attachments/{channel_id}/{attachment_id}/{filename}
served from the CDN bucket (FLUXER_S3_BUCKET_CDN).
"""

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError


def cdn_key(channel_id, attachment_id, filename):
    return f"attachments/{channel_id}/{attachment_id}/{filename}"


class S3Uploader:
    def __init__(self, cfg):
        self.bucket = cfg.s3_bucket_cdn
        self.client = boto3.client(
            "s3",
            endpoint_url=cfg.s3_endpoint,
            aws_access_key_id=cfg.s3_access_key,
            aws_secret_access_key=cfg.s3_secret_key,
            region_name=cfg.s3_region,
            config=BotoConfig(
                s3={"addressing_style": "path" if cfg.s3_force_path_style else "auto"}
            ),
        )

    def ensure_bucket(self):
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError:
            try:
                self.client.create_bucket(Bucket=self.bucket)
            except ClientError:
                pass

    def exists(self, key):
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def upload_file(self, local_path, key, content_type=None):
        extra = {"ContentType": content_type} if content_type else {}
        self.client.upload_file(local_path, self.bucket, key, ExtraArgs=extra)

    def delete_key(self, key):
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except ClientError:
            pass

    def delete_prefix(self, prefix):
        """Delete every object under a key prefix. Returns the count removed."""
        token = None
        removed = 0
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                kw["ContinuationToken"] = token
            r = self.client.list_objects_v2(**kw)
            objs = [{"Key": o["Key"]} for o in r.get("Contents", [])]
            if objs:
                self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": objs})
                removed += len(objs)
            if r.get("IsTruncated"):
                token = r.get("NextContinuationToken")
            else:
                break
        return removed
