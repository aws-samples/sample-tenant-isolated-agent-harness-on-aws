# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared demo client for the deployed split harness.

Used by the demo page (``demo/app.py``) and the demo script (``demo/run_demo.py``). It runs on
the operator's machine with the operator's AWS profile. It creates two demo tenants (one
Cognito user each, with ``custom:tenant_id`` set by the admin), seeds each tenant's jobs and
an upload, sends turns through the public API with each tenant's own ID token, and runs the
isolation checks in ``demo/checks.py``.

Passwords are generated per run and held in memory only.
"""

from __future__ import annotations

import json
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from shared import tenancy  # noqa: E402

STACK_NAME = "SplitHarness"
DONE_STATES = {"COMPLETED", "FAILED"}

TENANTS: dict[str, dict[str, Any]] = {
    "anycompany-plumbing": {
        "label": "AnyCompany Plumbing",
        "email": "owner@anycompany-plumbing.example",
        "jobs": [("J7", "open", "Hot water system replacement, 14 Rose St"),
                 ("J8", "scheduled", "Blocked drain, Unit 4, 2 Bay Rd"),
                 ("J9", "open", "Leaking mixer tap, 88 Hill Ave")],
        "prices": "sku,description,unit_price_aud\nHWS-250,250L electric hot water unit,1450.00\n"
                  "MXT-01,Mixer tap,189.50\nPIPE-15,15mm copper pipe per metre,12.80\n",
    },
    "example-corp-electrical": {
        "label": "Example Corp Electrical",
        "email": "owner@example-corp-electrical.example",
        "jobs": [("E1", "open", "Switchboard upgrade, 5 King St"),
                 ("E2", "open", "Install six LED downlights, 31 Park Lane")],
        "prices": "sku,description,unit_price_aud\nSWB-12,12-pole switchboard,640.00\n"
                  "LED-DL,LED downlight,38.90\nRCD-2P,2-pole RCD,96.00\n",
    },
}


class DemoError(RuntimeError):
    pass


class Demo:
    def __init__(self, profile: str, region: str = "ap-southeast-2", stack: str = STACK_NAME,
                 session: Any = None):
        import boto3  # lazy: tests can pass a fake session

        self.session = session or boto3.Session(profile_name=profile, region_name=region)
        self.region = region
        # boto3 sessions are not thread-safe, clients are: create each client once under a lock.
        self._clients: dict[str, Any] = {}
        self._clients_lock = threading.Lock()
        self._auth_lock = threading.RLock()
        self.outputs = self._stack_outputs(stack)
        self._passwords: dict[str, str] = {}
        self._tokens: dict[str, tuple[str, float]] = {}
        # Run the demo as the deployed least-privilege operator role, not the caller's own role, so
        # the sample never asks anyone to grant themselves break-glass. A test-injected session is
        # used as-is. The role is read with the caller's credentials, then assumed for everything else.
        operator = self.outputs.get("DemoOperatorRoleArn")
        if operator and session is None:
            creds = self.client("sts").assume_role(
                RoleArn=operator, RoleSessionName="split-harness-demo")["Credentials"]
            self.session = boto3.Session(
                aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"], region_name=region)
            with self._clients_lock:
                self._clients.clear()

    def client(self, name: str) -> Any:
        with self._clients_lock:
            if name not in self._clients:
                self._clients[name] = self.session.client(name)
            return self._clients[name]

    # ------------------------------------------------------------------ setup
    def _stack_outputs(self, stack: str) -> dict[str, str]:
        described = self.client("cloudformation").describe_stacks(StackName=stack)["Stacks"][0]
        outputs = {o["OutputKey"]: o["OutputValue"] for o in described.get("Outputs", [])}
        missing = {"ApiUrl", "UserPoolId", "UserPoolClientId", "SaasTable", "TenantBucket"} - outputs.keys()
        if missing:
            raise DemoError(f"stack {stack} is missing outputs {sorted(missing)}")
        return outputs

    @staticmethod
    def storage_id(tenant: str) -> str:
        return tenancy.storage_id_for(tenant)

    def ensure_user(self, tenant: str) -> None:
        with self._auth_lock:
            self._ensure_user(tenant)

    def _ensure_user(self, tenant: str) -> None:
        idp = self.client("cognito-idp")
        pool, email = self.outputs["UserPoolId"], TENANTS[tenant]["email"]
        try:
            idp.admin_create_user(UserPoolId=pool, Username=email, MessageAction="SUPPRESS", UserAttributes=[
                {"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"},
                {"Name": "custom:tenant_id", "Value": tenant}])
        except idp.exceptions.UsernameExistsException:
            pass
        password = self._passwords.get(tenant) or f"Dm{secrets.token_urlsafe(18)}9!"
        idp.admin_set_user_password(UserPoolId=pool, Username=email, Password=password, Permanent=True)
        self._passwords[tenant] = password

    def seed(self, tenant: str) -> dict:
        """The tenant's jobs (direct Gateway path) and a supplier price list (runtime path)."""
        sid = self.storage_id(tenant)
        ddb = self.client("dynamodb")
        for job_id, status, title in TENANTS[tenant]["jobs"]:
            ddb.put_item(TableName=self.outputs["SaasTable"], Item={
                "pk": {"S": tenancy.tenant_pk(sid)}, "sk": {"S": f"JOB#{job_id}"}, "jobId": {"S": job_id},
                "status": {"S": status}, "title": {"S": title}})
        key = tenancy.Layout.uploads_prefix(sid) + "prices.csv"
        self.client("s3").put_object(Bucket=self.outputs["TenantBucket"], Key=key,
                                             Body=TENANTS[tenant]["prices"].encode(), ContentType="text/csv")
        return {"tenant": tenant, "storageId": sid, "jobs": len(TENANTS[tenant]["jobs"]), "upload": key}

    def setup(self) -> list[dict]:
        done = []
        for tenant in TENANTS:
            self.ensure_user(tenant)
            done.append(self.seed(tenant))
        return done

    # ------------------------------------------------------------------ API calls
    def id_token(self, tenant: str) -> str:
        with self._auth_lock:
            return self._id_token(tenant)

    def _id_token(self, tenant: str) -> str:
        cached = self._tokens.get(tenant)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        if tenant not in self._passwords:
            self._ensure_user(tenant)
        auth = self.client("cognito-idp").admin_initiate_auth(
            # The admin password flow lives on the demo-only client (stack built with -c demo=true).
            UserPoolId=self.outputs["UserPoolId"],
            ClientId=self.outputs.get("DemoClientId") or self.outputs["UserPoolClientId"],
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": TENANTS[tenant]["email"], "PASSWORD": self._passwords[tenant]},
        )["AuthenticationResult"]
        self._tokens[tenant] = (auth["IdToken"], time.time() + int(auth.get("ExpiresIn", 3600)))
        return auth["IdToken"]

    def api(self, method: str, path: str, tenant: str | None, body: dict | None = None,
            token: str | None = None) -> tuple[int, dict]:
        headers = {"content-type": "application/json"}
        bearer = token if token is not None else (self.id_token(tenant) if tenant else None)
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        request = urllib.request.Request(self.outputs["ApiUrl"].rstrip("/") + path, method=method, headers=headers,
                                         data=json.dumps(body).encode() if body is not None else None)
        if urllib.parse.urlsplit(request.full_url).scheme != "https":
            raise DemoError("the stack's ApiUrl must use https")
        try:
            # Bandit B310: the check above allows only https.
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310  # nosec B310
                return response.status, _json(response.read())
        except urllib.error.HTTPError as err:
            return err.code, _json(err.read())

    def submit(self, tenant: str, message: str, request_id: str | None = None,
               extra: dict | None = None) -> tuple[int, dict]:
        request_id = request_id or new_request_id()
        return self.api("POST", "/turns", tenant, {"message": message, "requestId": request_id, **(extra or {})})

    def turn(self, tenant: str, request_id: str) -> tuple[int, dict]:
        return self.api("GET", f"/turns/{request_id}", tenant)

    def wait(self, tenant: str, request_id: str, timeout: float = 240, interval: float = 2.0) -> dict:
        deadline = time.time() + timeout
        while True:
            status, body = self.turn(tenant, request_id)
            if status == 200 and body.get("state") in DONE_STATES:
                return body
            if time.time() > deadline:
                return {**body, "state": body.get("state", "UNKNOWN"), "timedOut": True}
            time.sleep(interval)


def new_request_id() -> str:
    return "demo-" + uuid.uuid4().hex[:16]


def _json(raw: bytes) -> dict:
    try:
        value = json.loads(raw or b"{}")
    except ValueError:
        return {"raw": raw[:500].decode(errors="replace")}
    return value if isinstance(value, dict) else {"value": value}
