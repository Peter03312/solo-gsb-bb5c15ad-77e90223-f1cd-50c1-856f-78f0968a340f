"""Stable hashing: user -> bucket, user -> group."""
import hashlib

TOTAL_BUCKETS = 10000


def _hash_to_int(*parts: str) -> int:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return int(digest, 16) % TOTAL_BUCKETS


def user_bucket(layer_salt: str, user_key: str) -> int:
    """Deterministically map a user to a bucket in [0, 10000) within a layer."""
    return _hash_to_int(layer_salt, "bucket", user_key)


def choose_group(layer_salt: str, experiment_id: str, user_key: str, groups: list[dict]) -> str:
    """Deterministically pick a group by cumulative weights (weights sum to 10000).

    Uses an independent hash domain from bucketing so group split is not
    correlated with the user's bucket position.
    """
    point = _hash_to_int(layer_salt, "group", str(experiment_id), user_key)
    cumulative = 0
    for group in groups:
        cumulative += int(group["weight"])
        if point < cumulative:
            return group["name"]
    return groups[-1]["name"]  # unreachable when weights sum to TOTAL_BUCKETS
