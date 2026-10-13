from urllib.parse import urlsplit


class InvalidAcknowledgement(ValueError):
    pass


def flush(state, client, endpoint, token, max_batches=100, max_items=None):
    if max_items is not None and (type(max_items) is not int or max_items < 1):
        raise ValueError("max_items must be a positive integer")
    if urlsplit(endpoint).scheme != "https" or not token:
        raise ValueError("ingest requires HTTPS and a token")
    total = 0
    rows = 0
    exhausted = False
    sent_count = 0
    for _ in range(max_batches):
        batch_size = 5 if max_items is None else min(5, max_items - sent_count)
        if batch_size <= 0:
            break
        batch = state.pending(batch_size)
        if not batch:
            break
        sent_count += len(batch)
        response = client.post(endpoint, json={"listings": [item["listing"] for item in batch]}, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        try:
            body = response.json()
            accepted = body["accepted"]
            pending = body["pending"]
            written = body["rows_written"]
            exhausted = body["budget_exhausted"]
            identifiers = [item["id"] for item in accepted]
            valid = all(item["outcome"] in {"written", "noop"} for item in accepted)
            sent = {item["listing"]["id"] for item in batch}
            valid = valid and type(written) is int and written >= 0 and type(exhausted) is bool
            valid = valid and len(identifiers) == len(set(identifiers)) and len(pending) == len(set(pending))
            valid = valid and not set(identifiers).intersection(pending) and set(identifiers).union(pending) == sent
            if not valid:
                raise InvalidAcknowledgement("inconsistent individual acknowledgements")
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidAcknowledgement("invalid ingest response; batch remains pending") from exc
        state.acknowledge(batch, identifiers)
        total += len(identifiers)
        rows += written
        if exhausted or body.get("capacity_exhausted") or pending:
            break
    return {"accepted": total, "rows_written": rows, "budget_exhausted": exhausted, "pending": state.pending_count()}
