# SPDX-License-Identifier: AGPL-3.0-or-later
"""Snowflake ID generation matching Fluxer's implementation."""

from datetime import datetime

FLUXER_EPOCH = 1_420_070_400_000  # Jan 1, 2015 in milliseconds
WORKER_ID_BITS = 10
SEQUENCE_BITS = 12
WORKER_ID_SHIFT = SEQUENCE_BITS
TIMESTAMP_SHIFT = WORKER_ID_BITS + SEQUENCE_BITS
MAX_WORKER_ID = (1 << WORKER_ID_BITS) - 1
MAX_SEQUENCE = (1 << SEQUENCE_BITS) - 1


def create_snowflake(timestamp_ms: int, worker_id: int = 0, sequence: int = 0) -> int:
    """Create a Snowflake ID from a timestamp, worker ID, and sequence.

    Args:
        timestamp_ms: Milliseconds since Unix epoch
        worker_id: Worker ID (0-1023)
        sequence: Sequence number (0-4095)

    Returns:
        Snowflake ID as int
    """
    if not 0 <= worker_id <= MAX_WORKER_ID:
        raise ValueError(f"Worker ID must be between 0 and {MAX_WORKER_ID}")
    if not 0 <= sequence <= MAX_SEQUENCE:
        raise ValueError(f"Sequence must be between 0 and {MAX_SEQUENCE}")

    relative_timestamp = timestamp_ms - FLUXER_EPOCH
    if relative_timestamp < 0:
        raise ValueError("Timestamp must be on or after Fluxer epoch (Jan 1, 2015)")

    return (relative_timestamp << TIMESTAMP_SHIFT) | (worker_id << WORKER_ID_SHIFT) | sequence


def parse_snowflake(snowflake: int) -> dict:
    """Parse a Snowflake ID into its components.

    Returns:
        Dict with 'timestamp' (ms since epoch), 'worker_id', 'sequence'
    """
    sequence = snowflake & MAX_SEQUENCE
    worker_id = (snowflake >> WORKER_ID_SHIFT) & MAX_WORKER_ID
    relative_timestamp = snowflake >> TIMESTAMP_SHIFT
    timestamp_ms = relative_timestamp + FLUXER_EPOCH

    return {
        'timestamp': timestamp_ms,
        'worker_id': worker_id,
        'sequence': sequence,
    }


def snowflake_to_datetime(snowflake: int) -> datetime:
    """Convert a Snowflake ID to a datetime."""
    parts = parse_snowflake(snowflake)
    return datetime.fromtimestamp(parts['timestamp'] / 1000)


# Bucket size used to partition messages/reactions by time (10 days), matching
# packages/snowflake/src/SnowflakeBuckets.ts.
SNOWFLAKE_BUCKET_SIZE_MS = 10 * 24 * 60 * 60 * 1000  # 864_000_000


def make_bucket(snowflake: int) -> int:
    """Compute the message/reaction bucket for a snowflake.

    bucket = floor((snowflake >> 22) / SNOWFLAKE_BUCKET_SIZE_MS), where
    (snowflake >> 22) is the milliseconds since the Fluxer epoch.
    """
    relative_ms = snowflake >> TIMESTAMP_SHIFT
    return relative_ms // SNOWFLAKE_BUCKET_SIZE_MS
