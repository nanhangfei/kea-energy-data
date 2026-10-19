"""Hourly: name every project=kea resource that bills by the hour and has run too long.

Reports and deletes nothing -- tearing a session down is the operator's call
(deployment-plan.md §5). The three are what a session leaves billing: the NAT
gateway, the two RDS instances and, from Phase 3, the MWAA environment.
"""

from __future__ import annotations

import datetime as dt
import os

import boto3


def _is_kea(tags) -> bool:
    if isinstance(tags, dict):
        return tags.get("project") == "kea"
    return any(t.get("Key") == "project" and t.get("Value") == "kea" for t in tags or [])


def overdue(now: dt.datetime, max_hours: float) -> list[str]:
    cutoff = now - dt.timedelta(hours=max_hours)
    found = []

    ec2 = boto3.client("ec2")
    for page in ec2.get_paginator("describe_nat_gateways").paginate(
            Filters=[{"Name": "tag:project", "Values": ["kea"]},
                     {"Name": "state", "Values": ["pending", "available"]}]):
        for nat in page["NatGateways"]:
            if nat["CreateTime"] < cutoff:
                found.append(f"NAT gateway {nat['NatGatewayId']}, up since {nat['CreateTime']:%Y-%m-%d %H:%M} UTC")

    rds = boto3.client("rds")
    for page in rds.get_paginator("describe_db_instances").paginate():
        for db in page["DBInstances"]:
            created = db.get("InstanceCreateTime")
            if _is_kea(db.get("TagList")) and created and created < cutoff:
                found.append(f"RDS {db['DBInstanceIdentifier']}, up since {created:%Y-%m-%d %H:%M} UTC")

    mwaa = boto3.client("mwaa")
    for page in mwaa.get_paginator("list_environments").paginate():
        for name in page["Environments"]:
            env = mwaa.get_environment(Name=name)["Environment"]
            if _is_kea(env.get("Tags")) and env["CreatedAt"] < cutoff:
                found.append(f"MWAA {name}, up since {env['CreatedAt']:%Y-%m-%d %H:%M} UTC")

    return found


def handler(event, context):
    max_hours = float(os.environ["MAX_HOURS"])
    found = overdue(dt.datetime.now(dt.timezone.utc), max_hours)
    if found:
        boto3.client("sns").publish(
            TopicArn=os.environ["TOPIC_ARN"],
            Subject=f"Kea: a session has been up more than {max_hours:g} hours",
            Message="Still billing:\n  " + "\n  ".join(found)
                    + "\n\nIf the session is finished: make aws-down",
        )
    return {"overdue": found}
