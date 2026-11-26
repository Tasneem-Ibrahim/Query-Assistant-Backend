import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_server
from rag import access_control, admin_sessions, config, super_admin, user_auth, workspace
from auth_helpers import complete_mfa

OWNER = "55555555-5555-4555-8555-555555555555"
OTHER = "77777777-7777-4777-8777-777777777777"
HEADERS = {"X-CSRF-Protection": "1"}


class SuperAdminTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        settings = patch.multiple(
            config, DATA_DIR=self.root / "data", PDF_DIR=self.root / "pdfs",
            ADMIN_USERNAME="Admin", ADMIN_PASSWORD="test-admin-password",
            SUPER_ADMIN_USERNAME="Super Admin", SUPER_ADMIN_PASSWORD="test-super-password",
            AUTH_COOKIE_SECRET="test-signing-secret", AUTH_COOKIE_SECURE=False,
            AUTH_COOKIE_SAMESITE="lax", CLOUD_MODE=False,
        )
        settings.start()
        self.addCleanup(settings.stop)
        self.client = TestClient(api_server.app)
        self.addCleanup(self.client.close)

    def login(self, identity, password):
        response = self.client.post("/api/auth/login", headers=HEADERS,
                                    json={"identity": identity, "password": password})
        owner = admin_sessions.SUPER_ID if identity == config.SUPER_ADMIN_USERNAME else "local-admin"
        return complete_mfa(self.client, response, owner, HEADERS)

    def sign_in_super(self):
        response = self.login(config.SUPER_ADMIN_USERNAME, config.SUPER_ADMIN_PASSWORD)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["user"]["role"], "super_admin")

    def test_super_sign_in_refresh_and_logout_without_supabase(self):
        with patch.object(user_auth, "provider", side_effect=AssertionError("No provider call")):
            self.sign_in_super()
            self.assertEqual(self.client.get("/api/auth/session").json()["user"]["id"], admin_sessions.SUPER_ID)
            self.assertEqual(self.client.post("/api/auth/refresh", headers=HEADERS).json()["user"]["role"], "super_admin")
            self.assertEqual(self.client.post("/api/auth/logout", headers=HEADERS).status_code, 200)
            self.assertEqual(self.client.get("/api/auth/session").status_code, 401)

    def test_original_admin_cannot_manage_accounts_or_switch_workspaces(self):
        self.assertEqual(self.login(config.ADMIN_USERNAME, config.ADMIN_PASSWORD).status_code, 200)
        self.assertEqual(self.client.get("/api/super-admin/overview").status_code, 403)
        self.assertEqual(self.client.get("/api/domains", headers={"X-Workspace-Account": OWNER}).status_code, 403)
        for route in ("permission-settings", "login-logs", "login-logs.csv"):
            self.assertEqual(self.client.get("/api/super-admin/" + route).status_code, 403)

    def test_delegated_user_uploads_bind_selected_user_and_restrict_routes(self):
        self.login(config.ADMIN_USERNAME, config.ADMIN_PASSWORD)
        from rag import security_store
        security_store.write("account:local-admin", {
            "user_uploads.view": True, "user_uploads.add": True, "user_uploads.delete": True,
            "scope:role:user": True,
        }, 0)
        headers = {**HEADERS, "X-Workspace-Account": OWNER}
        domain = SimpleNamespace(pdf_dir=self.root / "selected-user", key="example")
        def ingest_selected(*args, **kwargs):
            self.assertEqual(workspace.owner_id(), OWNER)
            return {"name": args[1].name}

        with patch.object(super_admin, "auth", return_value={"id": OWNER}), patch.object(
            api_server, "list_domains", return_value=[],
        ), patch.object(api_server, "_domain", return_value=domain), patch.object(api_server.ingest, "delete_document",
            side_effect=lambda *_: self.assertEqual(workspace.owner_id(), OWNER)) as delete:
            self.assertEqual(self.client.get("/api/domains", headers=headers).status_code, 200)
            self.assertEqual(self.client.delete("/api/domains/example/documents/test", headers=headers).status_code, 200)
            delete.assert_called_once()
            self.assertEqual(self.client.get("/api/domains/example/documents/test", headers=headers).status_code, 403)
            self.assertEqual(self.client.post("/api/domains/example/ingest", headers=headers).status_code, 400)
            self.assertEqual(self.client.post("/api/domains/example/reset", headers=headers).status_code, 403)
            with patch.object(api_server.extract, "document_pdf"), patch.object(api_server.ingest, "ingest_pdf", side_effect=ingest_selected), patch("rag.admin_panel.cloud.rows", return_value=[]):
                files = {"file": ("delegated.pdf", b"test PDF", "application/pdf")}
                result = self.client.post("/api/domains/example/ingest", headers=headers, files=files)
                self.assertEqual(result.json(), {"type": "done", "report": {"name": "delegated.pdf"}})
                self.assertEqual(self.client.post("/api/domains/example/ingest", headers=headers, files=files).status_code, 409)
        with patch.object(super_admin, "auth", return_value={"id": OWNER, "app_metadata": {"qa_access": {"role": "admin"}}}):
            self.assertEqual(self.client.get("/api/domains", headers=headers).status_code, 403)

    def test_super_admin_can_restrict_original_admin_without_blocking_sign_in(self):
        self.sign_in_super()
        endpoint = "/api/super-admin/original-admin/permissions"
        with TestClient(api_server.app) as original:
            password = original.post("/api/auth/login", headers=HEADERS,
                                     json={"identity": config.ADMIN_USERNAME, "password": config.ADMIN_PASSWORD})
            self.assertEqual(complete_mfa(original, password, "local-admin", HEADERS).status_code, 200)
            self.assertEqual(original.put(endpoint, headers=HEADERS, json={"permissions": {}, "revision": 0}).status_code, 403)
            payload = {"permissions": {"bases.view": False, "approvals.edit": False}, "revision": 0}
            self.assertEqual(self.client.put(endpoint, headers=HEADERS, json=payload).status_code, 200)
            self.assertEqual(self.client.put(endpoint, headers=HEADERS, json=payload).status_code, 409)
            self.assertEqual(original.get("/api/auth/session").status_code, 200)
            self.assertEqual(original.get("/api/domains").status_code, 403)
            self.assertEqual(original.put("/api/admin/users/" + OWNER + "/permissions",
                                          headers=HEADERS, json={"policy": "allowed"}).status_code, 403)
            self.assertEqual(self.client.patch("/api/super-admin/accounts/local-admin", headers=HEADERS,
                                               json={"status": "inactive"}).status_code, 403)
            self.assertEqual(self.client.put(endpoint, headers=HEADERS, json={"permissions": {}, "revision": 1}).status_code, 200)
            self.assertTrue(original.get("/api/auth/session").json()["user"]["permissions"]["bases.view"])
            status = "/api/super-admin/original-admin/status"
            self.assertEqual(self.client.put(status, headers=HEADERS, json={"active": False, "revision": 0}).status_code, 200)
            self.assertTrue(original.get("/api/auth/session").json()["user"]["access_suspended"])
            self.assertEqual(original.get("/api/domains").status_code, 403)
            self.assertEqual(self.client.put(status, headers=HEADERS, json={"active": True, "revision": 1}).status_code, 200)
            self.assertNotIn("access_suspended", original.get("/api/auth/session").json()["user"])

    def test_upload_approval_switch_updates_only_admin_approval_permissions(self):
        self.sign_in_super()
        endpoint = "/api/super-admin/accounts/{}/upload-approval"
        response = self.client.put(endpoint.format("local-admin"), headers=HEADERS, json={"active": False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(access_control.original_admin_permissions()[0],
                         {"approvals.view": False, "approvals.edit": False})
        managed = {"id": OWNER, "app_metadata": {"qa_access": {"role": "admin", "permissions": {"bases.view": False}}}}
        with patch.object(super_admin, "auth", return_value=managed), patch.object(super_admin, "update", return_value={}) as update:
            response = self.client.put(endpoint.format(OWNER), headers=HEADERS, json={"active": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(update.call_args.kwargs["permissions"],
                         {"bases.view": False, "approvals.view": True, "approvals.edit": True})

    def test_user_details_and_upload_approvals_are_role_module_opt_ins(self):
        self.sign_in_super()
        endpoint = "/api/super-admin/permission-settings"
        keys = ("accounts.view", "approvals.view", "approvals.edit", "user_uploads.add")
        with patch.object(super_admin, "list_accounts", return_value=[]):
            self.assertEqual(set(self.client.get(endpoint).json()["opt_in_modules"]["user"]), {"accounts", "approvals", "user_uploads"})
        for revision, enabled in enumerate((True, False)):
            result = self.client.put(endpoint + "/user", headers=HEADERS, json={
                "modules": dict.fromkeys(("accounts", "approvals", "user_uploads"), enabled), "actions": {}, "revision": revision,
            })
            self.assertEqual(result.status_code, 200, result.text)
            for owner in (OWNER, OTHER):
                account = {"id": owner, "role": "user"}
                account["permissions"] = access_control.effective(account)
                self.assertEqual([access_control.allows(account, key) for key in keys], [enabled] * len(keys))
                self.assertFalse(access_control.allows(account, "approvals.delete"))
        with self.assertRaises(HTTPException):
            super_admin.validate_permissions("user", {"logs.export": True})

    def test_delegated_admin_role_defaults_module_gate_revocation_and_audit(self):
        self.sign_in_super()
        endpoint = "/api/super-admin/permission-settings"
        original = "/api/super-admin/original-admin/permissions"
        grant = {"permissions.view": True, "permissions.add": True, "permissions.edit": True, "documents.delete": False,
                 "logs.view": True, "revisions.view": True, "scope:role:user": True, "scope:policy:user": True}
        self.assertEqual(self.client.put(original, headers=HEADERS, json={"permissions": grant, "revision": 0}).status_code, 200)
        with TestClient(api_server.app) as admin:
            login = admin.post("/api/auth/login", headers=HEADERS,
                               json={"identity": config.ADMIN_USERNAME, "password": config.ADMIN_PASSWORD})
            self.assertEqual(complete_mfa(admin, login, "local-admin", HEADERS).status_code, 200)
            with patch.object(super_admin, "list_accounts", return_value=[{"id": OTHER, "role": "admin"}, {"id": OWNER, "role": "user"}]):
                snapshot = admin.get(endpoint).json()
            self.assertEqual(set(snapshot["editable_roles"]), {"user"})
            self.assertEqual([person["id"] for person in snapshot["accounts"]], [OWNER])
            self.assertNotIn("documents.delete", snapshot["delegation_scope"])
            def save(client, role, modules, actions, revision):
                return client.put(endpoint + "/" + role, headers=HEADERS,
                                  json={"modules": modules, "actions": actions, "revision": revision})

            self.assertEqual(save(admin, "admin", {}, {}, 0).status_code, 403)
            self.assertEqual(save(admin, "user", {"documents": False}, {}, 0).status_code, 403)
            self.assertEqual(save(admin, "user", {}, {"documents.delete": True}, 0).status_code, 403)
            self.assertEqual(save(admin, "user", {"bases": False}, {"bases.add": False}, 0).status_code, 200)
            settings = {"id": OWNER, "role": "user", "permissions": {"bases.view": True}}
            self.assertFalse(access_control.effective(settings)["bases.view"])
            self.assertEqual(save(admin, "user", {"bases": True}, {"bases.add": False}, 1).status_code, 200)
            self.assertTrue(access_control.effective(settings)["bases.view"])
            self.assertFalse(access_control.effective(settings)["bases.add"])

            self.assertEqual(save(self.client, "user", {"bases": False}, {"bases.add": False}, 2).status_code, 200)
            self.assertEqual(save(admin, "user", {"bases": True}, {"bases.add": False}, 3).status_code, 403)
            self.assertEqual(admin.get("/api/super-admin/login-logs").status_code, 200)
            with patch("rag.revisions.cloud._request", side_effect=ValueError("Offline test")):
                history = admin.get("/api/super-admin/revisions").json()["revisions"]
            edit = next(item for item in history if item["actor_id"] == "local-admin")
            self.assertEqual(edit["before_state"]["modules"], {"bases": False})
            self.assertEqual(edit["after_state"]["modules"], {"bases": True})
            reverse = "/api/super-admin/revisions/" + history[0]["revision_id"] + "/reverse"
            self.assertEqual(admin.post(reverse, headers=HEADERS).status_code, 403)
            self.assertEqual(self.client.post(reverse, headers=HEADERS).status_code, 200)
            self.assertEqual(self.client.put(original, headers=HEADERS, json={
                "permissions": {**grant, "revisions.edit": True}, "revision": 1,
            }).status_code, 200)
            self.assertEqual(admin.post(reverse, headers=HEADERS).status_code, 403)
            own_reverse = "/api/super-admin/revisions/" + edit["revision_id"] + "/reverse"
            self.assertEqual(admin.post(own_reverse, headers=HEADERS).status_code, 200)
            self.assertEqual(admin.post(own_reverse, headers=HEADERS).status_code, 409)
            events = admin.get("/api/super-admin/activity-logs/role-management").json()["events"]
            self.assertTrue(any(event["actor_id"] == "local-admin" for event in events))

            self.assertEqual(save(self.client, "admin", {"permissions": False}, {}, 0).status_code, 200)
            self.assertEqual(admin.get(endpoint).status_code, 403)
            self.assertEqual(save(admin, "user", {}, {}, 5).status_code, 403)
            self.assertEqual(admin.get("/api/auth/session").status_code, 200)

    def test_delegated_account_overrides_and_user_api_are_scoped(self):
        self.sign_in_super()
        policy = "/api/super-admin/permission-settings"
        self.assertEqual(self.client.put(policy + "/admin", headers=HEADERS, json={
            "modules": {}, "actions": {"permissions.view": True, "permissions.add": True, "permissions.edit": True, "documents.delete": False},
            "revision": 0,
        }).status_code, 200)
        self.assertEqual(self.client.put(policy + "/user", headers=HEADERS, json={
            "modules": {"accounts": True}, "actions": {"accounts.view": False}, "revision": 0,
        }).status_code, 200)

        managed = {"id": OTHER, "app_metadata": {"qa_access": {"role": "admin", "permissions": {"scope:" + OWNER: True}}}}
        person = {"id": OWNER, "app_metadata": {"qa_access": {"role": "user"}}}
        caller = managed

        async def authenticate(request):
            request.state.user = await user_auth.active_profile(caller)

        def provider(method, path, **kwargs):
            self.assertEqual(path, "/rest/v1/rpc/qa_update_delegated_account")
            self.assertIn("X-QA-Delegation", kwargs["headers"])
            self.assertEqual(kwargs["headers"]["X-QA-Revision-Actor"], OTHER)
            person["app_metadata"]["qa_access"] = kwargs["json"]["p_next"]
            return Mock(**{"json.return_value": True})

        target = "/api/super-admin/accounts/" + OWNER
        with patch.object(user_auth, "authenticate", side_effect=authenticate), patch.object(
            super_admin, "auth", side_effect=lambda method, path: managed if path.endswith(OTHER) else person,
        ), patch.object(access_control, "managed_admin_active", return_value=True), patch.object(
            super_admin.cloud, "_request", side_effect=provider,
        ) as update:
            for target_path, payload in (
                (target, {"role": "admin"}),
                (target, {"permissions": {"documents.delete": True}}),
                (target, {"permissions": {"accounts.view": True}}),
                (target, {"permissions": {"roles.edit": True}}),
                (target.replace(OWNER, OTHER), {"permissions": {}}),
            ):
                self.assertEqual(self.client.patch(target_path, headers=HEADERS, json=payload).status_code, 403)
            update.assert_not_called()
            self.assertEqual(self.client.patch(target, headers=HEADERS, json={
                "permissions": {"module:bases": False, "bases.add": False}, "expected_permissions": {},
            }).status_code, 200)
            self.assertEqual(self.client.patch(target, headers=HEADERS, json={
                "permissions": {}, "expected_permissions": {},
            }).status_code, 409)

            caller = person
            self.assertEqual(self.client.get("/api/domains").status_code, 403)
            self.assertEqual(self.client.get(policy).status_code, 403)
            self.assertEqual(self.client.get("/api/super-admin/login-logs").status_code, 403)
            self.assertEqual(self.client.patch(target, headers=HEADERS, json={"permissions": {}}).status_code, 403)

            caller = managed
            self.assertEqual(self.client.patch(target, headers=HEADERS, json={
                "permissions": {"bases.add": False},
            }).status_code, 200)
            caller = person
            self.assertEqual(self.client.get("/api/domains").status_code, 200)
            self.assertEqual(self.client.post("/api/domains", headers=HEADERS, json={"name": "Denied"}).status_code, 403)

    def test_cloud_delegation_uses_live_authority_snapshot_and_fails_closed_on_conflict(self):
        import json
        from rag import cloud, security_store
        settings = {
            "policy:admin": ({"actions": {"permissions.view": True, "permissions.edit": True}}, 4),
            "policy:user": ({}, 0),
            "account:local-admin": ({"scope:policy:user": True}, 2),
            "status:local-admin": ({"active": True}, 3),
        }

        async def authenticate(request):
            request.state.user = await user_auth.active_profile({
                "id": "local-admin", "_local_admin": True,
            })

        with patch.object(config, "CLOUD_MODE", True), patch.object(
            user_auth, "authenticate", side_effect=authenticate
        ), patch.object(security_store, "read", side_effect=settings.__getitem__), patch.object(
            cloud, "_request", side_effect=cloud.DelegationConflict("Permission settings changed.")
        ) as rpc:
            response = self.client.put("/api/super-admin/permission-settings/user", headers=HEADERS, json={
                "modules": {"bases": False}, "actions": {}, "revision": 0,
            })
        self.assertEqual(response.status_code, 409)
        self.assertEqual(rpc.call_args.args[1], "/rest/v1/rpc/qa_write_delegated_policy")
        snapshot = json.loads(rpc.call_args.kwargs["headers"]["X-QA-Delegation"])
        self.assertEqual(snapshot["state"], {key: revision for key, (_, revision) in settings.items()})
        self.assertEqual(snapshot["scope"], ["policy:user"])
        self.assertIn("permissions.edit", snapshot["allowed"])

    def test_audit_journal_operations_are_scoped_and_preserve_originals(self):
        from rag import audit_journal, revisions, security_store
        operator = "66666666-6666-4666-8666-666666666666"
        security_store.log({"id": OWNER, "role": "user"}, "signed_in")
        security_store.log({"id": OTHER, "role": "user"}, "signed_out")
        with revisions.database() as db:
            db.execute("""insert into setting_revisions
                (created_at,kind,target,actor_id,before_state,after_state)
                values('2026-11-26T00:00:00+00:00','account',?,?,'{}','{}')""", (OWNER, admin_sessions.SUPER_ID))
        grant = {key: True for key in ("logs.view", "logs.add", "logs.edit", "logs.delete",
                                      "revisions.view", "revisions.add", "revisions.delete")}
        admin = {"id": operator, "app_metadata": {"qa_access": {"role": "admin",
                 "permissions": {**grant, "scope:" + OWNER: True}}}}
        user = {"id": OTHER, "app_metadata": {"qa_access": {"role": "user", "permissions": {
            "logs.view": True, "logs.add": True, "revisions.view": True, "revisions.add": True, "scope:" + OWNER: True,
        }}}}
        caller = admin

        async def authenticate(request):
            request.state.user = ({"role": "super_admin", "id": admin_sessions.SUPER_ID}
                                  if caller is None else await user_auth.active_profile(caller))

        def auth(method, path):
            owner = path.split("/")[-1]
            return admin if owner == operator else user if owner == OTHER else {"id": OWNER}

        root = "/api/super-admin/audit-journal/"
        note = {"note": "Verified original record"}
        with patch.object(user_auth, "authenticate", side_effect=authenticate), patch.object(
            super_admin, "auth", side_effect=auth
        ), patch.object(access_control, "managed_admin_active", return_value=True):
            first = self.client.post(root + "logs", headers=HEADERS, json={**note, "subject": OWNER})
            self.assertEqual(first.status_code, 201, first.text)
            self.assertEqual(self.client.post(root + "logs", headers=HEADERS,
                             json={**note, "subject": OTHER}).status_code, 403)
            self.assertEqual(self.client.patch(root + "logs/login:2", headers=HEADERS, json=note).status_code, 403)
            self.assertEqual(self.client.patch(root + "logs/login:1", headers=HEADERS, json=note).status_code, 200)
            failed = self.client.request("DELETE", root + "logs/login:999", headers=HEADERS, json=note)
            self.assertEqual(failed.status_code, 404)
            archived = self.client.request("DELETE", root + "logs/login:1", headers=HEADERS, json=note)
            self.assertEqual(archived.status_code, 200)
            duplicate = self.client.request("DELETE", root + "logs/login:1", headers=HEADERS, json=note)
            self.assertEqual(duplicate.status_code, 409)
            annotations = self.client.post(root + "logs/annotations", headers=HEADERS, json={"references": ["login:1"]})
            self.assertTrue(annotations.json()["login:1"]["archived"])
            self.assertEqual(annotations.json()["login:1"]["correction"]["note"], note["note"])
            raw = self.client.get("/api/super-admin/login-logs").json()["events"]
            self.assertEqual(next(row for row in raw if row["id"] == 1)["outcome"], "signed_in")
            self.assertIn("signed_in", self.client.get("/api/super-admin/login-logs.csv").text)

            caller = user
            manual = self.client.post(root + "revisions", headers=HEADERS, json={**note, "subject": OWNER})
            self.assertEqual(manual.status_code, 201)
            self.assertEqual(self.client.post(root + "logs/login:1/restore", headers=HEADERS, json=note).status_code, 403)
            self.assertEqual(self.client.request("DELETE", root + "revisions/revision:L:1", headers=HEADERS, json=note).status_code, 403)
            caller = admin
            self.assertEqual(self.client.request("DELETE", root + "revisions/revision:L:1", headers=HEADERS, json=note).status_code, 200)
            caller = None
            self.assertEqual(self.client.post(root + "logs/login:1/restore", headers=HEADERS, json=note).status_code, 200)
            self.assertEqual(self.client.post(root + "revisions", headers=HEADERS, json=note).status_code, 201)

        with revisions.database() as db:
            self.assertEqual(db.execute("select target from setting_revisions where id=1").fetchone()["target"], OWNER)
        self.assertFalse(audit_journal.states("logs", ["login:1"])["login:1"]["archived"])
        self.assertEqual([entry["operation"] for entry in audit_journal.history("logs")["entries"]],
                         ["restore", "archive", "correct", "add"])
        events = security_store.activity_events("role-management", 50)
        self.assertEqual(len(events), 7)
        self.assertEqual({event["actor_id"] for event in events}, {operator, OTHER, admin_sessions.SUPER_ID})

    def test_permission_add_edit_delete_are_independent_for_scoped_user(self):
        person = {"id": OTHER, "app_metadata": {"qa_access": {"role": "user"}}}
        owner = {"id": OWNER, "app_metadata": {"qa_access": {"role": "user", "permissions": {
            "permissions.view": True, "permissions.add": True, "scope:" + OTHER: True,
        }}}}
        grants = owner["app_metadata"]["qa_access"]["permissions"]

        async def authenticate(request):
            request.state.user = await user_auth.active_profile(owner)

        def persist(method, path, **kwargs):
            settings = kwargs["json"]["p_next"]
            person["app_metadata"]["qa_access"] = settings
            return Mock(**{"json.return_value": True})

        with patch.object(user_auth, "authenticate", side_effect=authenticate), patch.object(
            super_admin, "auth", side_effect=lambda method, path: owner if path.endswith(OWNER) else person,
        ), patch.object(super_admin.cloud, "_request", side_effect=persist):
            path = "/api/super-admin/accounts/" + OTHER
            def save(values):
                return self.client.patch(path, headers=HEADERS, json={"permissions": values})

            self.assertEqual(save({"bases.add": True}).status_code, 200)
            self.assertEqual(save({"bases.add": False}).status_code, 403)
            self.assertEqual(save({}).status_code, 403)
            grants.update({"permissions.add": False, "permissions.edit": True})
            self.assertEqual(save({"bases.add": False}).status_code, 200)
            self.assertEqual(save({"bases.add": False, "bases.view": True}).status_code, 403)
            grants.update({"permissions.edit": False, "permissions.delete": True})
            self.assertEqual(save({}).status_code, 200)
            self.assertEqual(person["app_metadata"]["qa_access"]["permissions"], {})
            self.assertEqual(save({"bases.add": False}).status_code, 403)

    def test_user_details_add_creates_user_without_role_management(self):
        actor = {"id": OTHER, "app_metadata": {"qa_access": {"role": "user", "permissions": {
            "module:accounts": True, "accounts.view": True, "accounts.add": True, "scope:role:user": True,
        }}}}

        async def authenticate(request):
            request.state.user = await user_auth.active_profile(actor)

        def auth(method, path, **kwargs):
            if method == "GET":
                return actor
            return {"id": OWNER, "email": "new@example.com", "app_metadata": kwargs["json"]["app_metadata"]}

        payload = {"name": "New User", "email": "new@example.com", "password": "test-password"}
        with patch.object(user_auth, "authenticate", side_effect=authenticate), patch.object(
            super_admin, "auth", side_effect=auth
        ), patch.object(super_admin.cloud, "_request"):
            result = self.client.post("/api/super-admin/user-details", headers=HEADERS, json=payload)
            self.assertEqual(result.status_code, 201, result.text)
            self.assertEqual(result.json()["role"], "user")
            self.assertEqual(self.client.post("/api/super-admin/accounts", headers=HEADERS, json=payload).status_code, 403)
            self.assertEqual(self.client.post("/api/super-admin/user-details", headers=HEADERS,
                             json={**payload, "role": "admin"}).status_code, 422)
            del actor["app_metadata"]["qa_access"]["permissions"]["scope:role:user"]
            self.assertEqual(self.client.post("/api/super-admin/user-details", headers=HEADERS, json=payload).status_code, 403)

    def test_same_catalog_and_module_switches_for_both_roles(self):
        self.sign_in_super()
        endpoint = "/api/super-admin/permission-settings"
        with patch.object(super_admin, "list_accounts", return_value=[]):
            catalog = self.client.get(endpoint).json()
        self.assertEqual(catalog["assignable"]["admin"], catalog["assignable"]["user"])
        actions = dict.fromkeys(catalog["assignable"]["user"], True)
        modules = dict.fromkeys(catalog["resources"], True)
        for role in ("admin", "user"):
            person = {"id": OWNER, "role": role, "permissions": {}}
            for revision, enabled in enumerate((True, False, True)):
                values = {**modules, "roles": enabled}
                saved = self.client.put(endpoint + "/" + role, headers=HEADERS,
                    json={"revision": revision, "modules": values, "actions": actions})
                self.assertEqual(saved.status_code, 200, saved.text)
                effective = access_control.effective(person)
                self.assertEqual(effective["roles.edit"], enabled)
                self.assertTrue(effective["permissions.edit"])
                self.assertTrue(effective["logs.view"])
                self.assertTrue(effective["logs.delete"])
        explicit = {"actions": {"roles.view": True}, "modules": {"roles": False}}
        self.assertFalse(access_control.effective({"id": OWNER, "role": "user", "permissions": {
            "module:roles": True, "roles.view": True,
        }}, explicit)["roles.view"])

    def test_user_can_manage_scoped_roles_without_a_role_change(self):
        third = "66666666-6666-4666-8666-666666666666"
        actor = {"id": OWNER, "app_metadata": {"qa_access": {"role": "user"}}}
        target = {"id": OTHER, "app_metadata": {"qa_access": {"role": "user"}}}
        outside = {"id": third, "app_metadata": {"qa_access": {"role": "user"}}}
        people = {person["id"]: person for person in (actor, target, outside)}
        endpoint = "/api/super-admin/accounts/"
        actor_provider = actor

        async def authenticate(request):
            request.state.user = ({"role": "super_admin", "id": admin_sessions.SUPER_ID}
                                  if actor_provider is None else await user_auth.active_profile(actor_provider))

        def auth(method, path):
            return {"users": list(people.values())} if not path else people[path.removeprefix("/")]

        def save(method, path, **kwargs):
            body = kwargs["json"]
            person = people[body["p_user"]]
            self.assertEqual(person["app_metadata"]["qa_access"], body["p_previous"])
            person["app_metadata"]["qa_access"] = body["p_next"]
            return Mock(**{"json.return_value": True})

        partial = {"roles.view": True, "permissions.view": True, "permissions.add": True, "permissions.edit": True,
                   "scope:" + OTHER: True}
        full = {key: True for key in access_control.grant_scope({"role": "super_admin"})}
        full.update({"module:" + resource: True for resource in access_control.RESOURCES})
        full["scope:" + OTHER] = True

        with patch.object(user_auth, "authenticate", side_effect=authenticate), patch.object(
            super_admin, "auth", side_effect=auth
        ), patch.object(super_admin.cloud, "_request", side_effect=save), patch.object(
            access_control, "managed_admin_active", return_value=True
        ), patch.object(super_admin, "list_accounts", side_effect=lambda: [super_admin.account(x) for x in people.values()]):
            actor_provider = None
            self.assertEqual(self.client.patch(endpoint + OWNER, headers=HEADERS,
                json={"permissions": partial}).status_code, 200)
            actor_provider = actor
            listing = self.client.get("/api/super-admin/role-accounts")
            self.assertEqual([row["id"] for row in listing.json()["accounts"]], [OTHER])
            self.assertEqual(self.client.patch(endpoint + OTHER, headers=HEADERS,
                json={"role": "admin"}).status_code, 403)
            self.assertEqual(self.client.patch(endpoint + OTHER, headers=HEADERS,
                json={"permissions": {"roles.edit": True}}).status_code, 403)
            self.assertEqual(self.client.patch(endpoint + OTHER, headers=HEADERS,
                json={"permissions": {"bases.add": False}}).status_code, 200)

            actor_provider = None
            self.assertEqual(self.client.patch(endpoint + OWNER, headers=HEADERS,
                json={"permissions": full}).status_code, 200)
            actor_provider = actor
            self.assertEqual(self.client.get("/api/auth/session").json()["user"]["role"], "user")
            for owner in (OWNER, third, admin_sessions.SUPER_ID):
                self.assertEqual(self.client.patch(endpoint + owner, headers=HEADERS,
                    json={"role": "admin"}).status_code, 403)
            for new_role in ("admin", "user"):
                result = self.client.patch(endpoint + OTHER, headers=HEADERS, json={"role": new_role})
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["role"], new_role)
            self.assertEqual(self.client.patch(endpoint + OTHER, headers=HEADERS,
                json={"permissions": {"scope:" + OWNER: True}}).status_code, 403)
            self.assertEqual(self.client.put("/api/super-admin/roles/admin", headers=HEADERS,
                json={"active": False}).status_code, 403)

            actor_provider = None
            self.assertEqual(self.client.patch(endpoint + OWNER, headers=HEADERS,
                json={"permissions": {**full, "module:roles": False}}).status_code, 200)
            actor_provider = actor
            self.assertEqual(self.client.get("/api/super-admin/role-accounts").status_code, 403)
            self.assertEqual(self.client.patch(endpoint + OTHER, headers=HEADERS,
                json={"role": "admin"}).status_code, 403)
            events = self.client.get("/api/super-admin/activity-logs/role-management").json()["events"]
            self.assertEqual(sum(row["actor_id"] == OWNER and row["action"] == "Changed account role"
                                 for row in events), 2)

    def test_super_admin_denial_supersedes_a_delegated_override_and_legacy_direct_upload(self):
        from rag import admin_panel, security_store
        identity = {"id": OWNER, "app_metadata": {"qa_access": {
            "permissions": {"user_uploads.add": True, "bases.add": True},
            "delegated_overrides": ["user_uploads.add", "bases.add"],
        }}}
        async def authenticate(request):
            request.state.user = await user_auth.active_profile(identity)

        with patch.object(user_auth, "authenticate", side_effect=authenticate), patch.object(
            admin_panel, "policy", return_value="allowed"
        ), patch.object(admin_panel.cloud, "rows", return_value=[]):
            session = self.client.get("/api/auth/session").json()["user"]
            self.assertTrue(session["permissions"]["bases.add"])
            self.assertEqual(self.client.get("/api/upload-requests").json()["policy"], "allowed")
            self.assertTrue(security_store.write("policy:user", {
                "modules": {"user_uploads": True},
                "actions": {"user_uploads.add": False, "bases.add": False},
            }, 0))
            session = self.client.get("/api/auth/session").json()["user"]
            self.assertFalse(session["permissions"]["bases.add"])
            self.assertFalse(session["permissions"]["user_uploads.add"])
            self.assertEqual(self.client.get("/api/upload-requests").json()["policy"], "approval_required")

    def test_enabled_user_can_review_other_uploads_but_not_their_own(self):
        async def authenticate(request):
            account = {"id": OWNER, "role": "user"}
            account["permissions"] = access_control.effective(account, {"modules": {"approvals": True}})
            request.state.user = account

        with patch.object(user_auth, "authenticate", side_effect=authenticate), patch.object(
            super_admin.cloud, "rows", side_effect=lambda table, **filters: [{"id": OWNER}] if filters.get("id") == "eq." + OWNER else []
        ), patch.object(super_admin.cloud, "_request") as provider:
            endpoint = "/api/admin/upload-requests/"
            body = {"status": "approved", "reason": "Reviewed"}
            self.assertEqual(self.client.put(endpoint + OWNER, headers=HEADERS, json=body).status_code, 403)
            provider.assert_not_called()
            provider.return_value.json.return_value = []
            self.assertEqual(self.client.put(endpoint + OTHER, headers=HEADERS, json=body).status_code, 409)
            self.assertEqual(provider.call_args.args, ("POST", "/rest/v1/rpc/qa_decide_saved_upload"))

    def test_admin_approval_add_and_delete_respect_permission_and_preserve_workspace_files(self):
        self.login(config.ADMIN_USERNAME, config.ADMIN_PASSWORD)
        from rag import domains, saved_uploads, security_store
        add = "/api/admin/upload-requests/for/" + OWNER
        remove = "/api/admin/upload-requests/" + OTHER
        self.assertEqual(self.client.get(add, headers=HEADERS).status_code, 403)
        self.assertEqual(self.client.delete(remove, headers=HEADERS).status_code, 403)
        security_store.write("account:local-admin", {"approvals.add": True, "approvals.delete": True, "scope:role:user": True}, 0)

        def save(*args, **options):
            self.assertEqual(workspace.owner_id(), OWNER)
            self.assertTrue(options["request_approval"])
            return {"status": "pending"}

        with patch.object(super_admin, "auth", return_value={"id": OWNER}), patch.object(
            domains, "get_domain", return_value=SimpleNamespace(key="example")
        ), patch.object(saved_uploads, "save", side_effect=save):
            response = self.client.post(add, headers=HEADERS, data={"domain": "example"},
                                        files={"file": ("request.pdf", b"test", "application/pdf")})
        self.assertEqual(response.json(), {"status": "pending"})
        with patch.object(super_admin.cloud, "_request") as provider:
            provider.return_value.json.return_value = [{"id": OTHER}]
            self.assertEqual(self.client.delete(remove, headers=HEADERS).status_code, 200)
            provider.assert_called_once()
            self.assertEqual(provider.call_args.args, ("PATCH", "/rest/v1/qa_upload_requests"))
            self.assertEqual(provider.call_args.kwargs["json"]["status"], "expired")

    def test_settings_persist_and_login_events_export(self):
        self.sign_in_super()
        endpoint = "/api/super-admin/permission-settings"
        policy = {"modules": {"bases": False}, "actions": {"documents.edit": False}, "revision": 0}
        self.assertEqual(self.client.put(endpoint + "/user", headers=HEADERS, json=policy).status_code, 200)
        self.assertEqual(self.client.put(endpoint + "/user", headers=HEADERS, json=policy).status_code, 409)
        self.assertEqual(self.client.put(endpoint + "/admin", headers=HEADERS, json={**policy, "modules": {"unknown": True}}).status_code, 400)
        with patch.object(super_admin, "list_accounts", return_value=[]):
            self.assertEqual(self.client.get(endpoint).json()["policies"]["user"], {**policy, "revision": 1})
        logs = self.client.get("/api/super-admin/login-logs").json()["events"]
        self.assertEqual([(row["account_id"], row["outcome"]) for row in logs],
                         [(admin_sessions.SUPER_ID, "signed_in")])
        export = self.client.get("/api/super-admin/login-logs.csv")
        self.assertEqual(export.status_code, 200)
        self.assertIn("attachment", export.headers["content-disposition"])
        self.assertIn("id,account_id,created_at,role,outcome", export.text)
        self.assertIn(admin_sessions.SUPER_ID, export.text)

    def test_log_management_records_activity_and_exports_logout(self):
        self.sign_in_super()
        prefix = "/api/super-admin/"
        self.assertEqual(self.client.post("/api/domains", headers=HEADERS, json={"name": "Research"}).status_code, 201)
        self.assertEqual(self.client.put(prefix + "permission-settings/user", headers=HEADERS,
                                         json={"modules": {"bases": False}, "actions": {}, "revision": 0}).status_code, 200)
        self.assertEqual(self.client.put(prefix + "original-admin/status", headers=HEADERS,
                                         json={"active": False, "revision": 0}).status_code, 200)
        expected = {
            "user-details": "Updated original Admin access status",
            "knowledge-base": "Created knowledge base",
            "role-management": "Updated role permissions",
        }
        for category, action in expected.items():
            response = self.client.get(prefix + "activity-logs/" + category)
            self.assertEqual(response.status_code, 200)
            self.assertEqual([(event["actor_id"], event["action"]) for event in response.json()["events"]],
                             [(admin_sessions.SUPER_ID, action)])
        self.assertEqual(self.client.post("/api/auth/logout", headers=HEADERS).status_code, 200)
        self.assertEqual(self.client.get(prefix + "activity-logs/knowledge-base").status_code, 401)
        self.sign_in_super()
        self.assertIn("signed_out", self.client.get(prefix + "login-logs.csv").text)

    def test_revision_diff_reverse_conflict_and_audit(self):
        self.sign_in_super()
        root = "/api/super-admin/"
        endpoint = root + "permission-settings/user"
        for revision, enabled in enumerate((True, False)):
            response = self.client.put(endpoint, headers=HEADERS,
                json={"modules": {"user_uploads": enabled}, "actions": {}, "revision": revision})
            self.assertEqual(response.status_code, 200, response.text)
        with patch("rag.revisions.cloud._request", side_effect=ValueError("Offline test")):
            history = self.client.get(root + "revisions").json()["revisions"]
        latest, earlier = history
        self.assertEqual(latest["before_state"]["modules"], {"user_uploads": True})
        self.assertEqual(latest["after_state"]["modules"], {"user_uploads": False})
        reverse = root + "revisions/{}/reverse"
        self.assertEqual(self.client.post(reverse.format(earlier["revision_id"]), headers=HEADERS).status_code, 409)
        result = self.client.post(reverse.format(latest["revision_id"]), headers=HEADERS)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.client.post(reverse.format(latest["revision_id"]), headers=HEADERS).status_code, 409)
        self.assertEqual(access_control.policy("user")[0]["modules"], {"user_uploads": True})
        with patch("rag.revisions.cloud._request", side_effect=ValueError("Offline test")):
            newest = self.client.get(root + "revisions").json()["revisions"][0]
        self.assertEqual(newest["reversed_revision"], latest["revision_id"])
        self.assertEqual(newest["revision_id"], result.json()["revision_id"])
        self.assertEqual(newest["actor_id"], admin_sessions.SUPER_ID)
        logs = self.client.get(root + "activity-logs/role-management").json()["events"]
        self.assertEqual(logs[0]["action"], "Reversed " + latest["revision_id"])

    def test_original_admin_cannot_read_or_reverse_revisions(self):
        self.assertEqual(self.login(config.ADMIN_USERNAME, config.ADMIN_PASSWORD).status_code, 200)
        self.assertEqual(self.client.get("/api/super-admin/revisions").status_code, 403)
        self.assertEqual(self.client.post("/api/super-admin/revisions/RV-L000001/reverse", headers=HEADERS).status_code, 403)

    def test_revision_restores_original_admin_access_and_logs_account_activity(self):
        self.sign_in_super()
        root = "/api/super-admin/"
        response = self.client.put(root + "original-admin/status", headers=HEADERS,
                                   json={"active": False, "revision": 0})
        self.assertEqual(response.status_code, 200)
        response = self.client.post(root + "revisions/RV-L000001/reverse", headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(access_control.original_admin_status()[0]["active"])
        logs = self.client.get(root + "activity-logs/user-details").json()["events"]
        self.assertEqual((logs[0]["target"], logs[0]["action"]), ("local-admin", "Reversed RV-L000001"))

    def test_cloud_revision_reverse_uses_transaction_rpc_and_local_audit_view(self):
        self.sign_in_super()
        with patch("rag.revisions.lookup", return_value=None), patch("rag.revisions.cloud._request") as rpc:
            rpc.return_value.json.return_value = {"id": 52, "category": "user-details", "target": OWNER}
            response = self.client.post("/api/super-admin/revisions/RV-C000051/reverse", headers=HEADERS)
            self.assertEqual(response.json(), {"revision_id": "RV-C000052"})
            rpc.assert_called_once_with("POST", "/rest/v1/rpc/qa_reverse_setting_revision",
                                        json={"p_id": 51, "p_actor": admin_sessions.SUPER_ID})
        logs = self.client.get("/api/super-admin/activity-logs/user-details").json()["events"]
        self.assertEqual((logs[0]["target"], logs[0]["action"]), (OWNER, "Reversed RV-C000051"))

    def test_super_workspace_switch_scopes_existing_routes(self):
        self.sign_in_super()
        with patch.object(super_admin, "auth", return_value={"id": OWNER}), patch.object(
            api_server, "list_domains", side_effect=lambda: self.assertEqual(workspace.owner_id(), OWNER) or []
        ):
            self.assertEqual(self.client.get("/api/domains", headers={"X-Workspace-Account": OWNER}).status_code, 200)
        self.assertEqual(workspace.owner_id(), "admin")

    def test_only_server_metadata_controls_roles_and_deactivation(self):
        self.assertEqual(user_auth.profile({"id": OWNER, "user_metadata": {"role": "super_admin"}})["role"], "user")
        self.assertEqual(user_auth.profile({"id": OWNER, "app_metadata": {"qa_access": {"role": "admin"}}})["role"], "admin")
        with self.assertRaises(HTTPException) as rejected:
            user_auth.profile({"id": OWNER, "app_metadata": {"qa_access": {"status": "inactive"}}})
        self.assertEqual(rejected.exception.status_code, 403)

    def test_disabled_permissions_block_rag_routes_before_they_run(self):
        async def authenticate(request):
            request.state.user = {"id": OWNER, "role": "user", "permissions": {
                "bases.edit": False, "documents.view": False, "documents.delete": False}}
        with patch.object(user_auth, "authenticate", side_effect=authenticate):
            for method, path in (("PATCH", "/domains/custom_base"), ("POST", "/domains/custom_base/ask"),
                                 ("DELETE", "/domains/custom_base")):
                response = self.client.request(method, "/api" + path, headers=HEADERS)
                self.assertEqual(response.status_code, 403)

    def test_admin_role_management_stays_protected(self):
        self.assertTrue(access_control.available("admin", "roles.edit"))
        self.assertFalse(access_control.defaults("admin")["roles.edit"])
        self.sign_in_super()
        with patch.object(super_admin, "auth") as provider:
            response = self.client.patch("/api/super-admin/accounts/" + admin_sessions.SUPER_ID,
                                         headers=HEADERS, json={"status": "inactive"})
            self.assertEqual(response.status_code, 403)
            provider.assert_not_called()

    def test_permanent_deletion_requires_deactivation_and_confirmation(self):
        self.sign_in_super()
        with patch.object(super_admin, "auth", return_value={"id": OWNER}), patch.object(super_admin, "purge_workspace") as purge:
            endpoint = "/api/super-admin/accounts/" + OWNER
            self.assertEqual(self.client.delete(endpoint + "?confirmation=cancel", headers=HEADERS).status_code, 400)
            self.assertEqual(self.client.delete(endpoint + "?confirmation=DELETE", headers=HEADERS).status_code, 409)
            purge.assert_not_called()

    def test_inactive_account_deletion_transitions_before_purge(self):
        self.sign_in_super()
        calls = []

        def auth(method, path):
            calls.append(method)
            return {"id": OWNER, "app_metadata": {"qa_access": {"status": "inactive"}}}

        with patch.object(super_admin, "auth", side_effect=auth), patch.object(
            super_admin, "update", side_effect=lambda *args, **kwargs: calls.append(kwargs["status"])
        ), patch.object(super_admin, "purge_workspace", side_effect=lambda owner: calls.append("purge")), patch.object(
            super_admin, "audit", side_effect=lambda *args: calls.append("audit")
        ):
            endpoint = "/api/super-admin/accounts/" + OWNER + "?confirmation=DELETE"
            response = self.client.delete(endpoint, headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, ["GET", "deleting", "purge", "DELETE", "audit"])

    def test_active_workspace_write_blocks_permanent_deletion(self):
        self.sign_in_super()
        self.assertTrue(workspace.begin_write(OWNER))
        try:
            with patch.object(super_admin, "auth", return_value={"id": OWNER}), patch.object(
                super_admin, "purge_workspace"
            ) as purge:
                endpoint = "/api/super-admin/accounts/" + OWNER + "?confirmation=DELETE"
                self.assertEqual(self.client.delete(endpoint, headers=HEADERS).status_code, 409)
                purge.assert_not_called()
        finally:
            workspace.end_write(OWNER)

    def test_managed_admin_role_inactive_blocks_sessions_and_assignment(self):
        managed = {"id": OWNER, "app_metadata": {"qa_access": {"role": "admin"}}}
        with patch.object(access_control, "managed_admin_active", return_value=False):
            from anyio import run
            with self.assertRaises(HTTPException) as rejected:
                run(user_auth.active_profile, managed)
            self.assertEqual(rejected.exception.status_code, 403)
            self.assertEqual(self.login(config.ADMIN_USERNAME, config.ADMIN_PASSWORD).status_code, 200)
            self.assertEqual(self.client.put("/api/super-admin/roles/admin", headers=HEADERS,
                                             json={"active": False}).status_code, 403)
            self.sign_in_super()
            result = self.client.post("/api/super-admin/accounts", headers=HEADERS, json={
                "name": "New admin", "email": "new@example.test", "password": "test-password", "role": "admin",
            })
            self.assertEqual(result.status_code, 409)

    def test_super_admin_role_toggle_audit_rpc(self):
        self.sign_in_super()
        with patch("rag.super_admin.cloud._request") as rpc:
            response = self.client.put("/api/super-admin/roles/admin", headers=HEADERS, json={"active": False})
        self.assertEqual(response.json(), {"active": False})
        rpc.assert_called_once_with("POST", "/rest/v1/rpc/qa_set_admin_role_active",
                                   headers={"X-QA-Revision-Actor": admin_sessions.SUPER_ID}, json={"p_active": False})

    def test_create_and_invite_apply_permissions_before_returning(self):
        self.sign_in_super()
        user = {"id": OWNER, "email": "new@example.test"}
        data = {"name": "New user", "email": user["email"], "role": "user", "permissions": {"bases.add": False}}
        with patch("rag.super_admin.cloud._request"), patch.object(
            super_admin, "auth", return_value=user,
        ) as auth:
            result = self.client.post("/api/super-admin/accounts", headers=HEADERS,
                                      json={**data, "password": "test-password"})
        self.assertEqual(result.status_code, 201)
        self.assertEqual(auth.call_args.kwargs["json"]["app_metadata"]["qa_access"],
                         {"role": "user", "permissions": {"bases.add": False}, "created_by": admin_sessions.SUPER_ID})

        with patch("rag.super_admin.cloud._request"), patch.object(
            super_admin, "auth", return_value={**user, "hashed_token": "test-invite-token"},
        ) as auth, patch.object(
            super_admin, "update", return_value=user,
        ) as update:
            result = self.client.post("/api/super-admin/accounts", headers=HEADERS, json={**data, "method": "invite"})
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.json()["invite_token"], user_auth.sign_invite("test-invite-token"))
        self.assertEqual({key: value for key, value in update.call_args.kwargs.items() if key != "revision_request"},
                         {"role": "user", "invited": True, "permissions": {"bases.add": False}})
        self.assertEqual(auth.call_args.kwargs["endpoint"], "/admin/generate_link")
        self.assertNotIn("password", auth.call_args.kwargs["json"])

    def test_tampered_invitation_is_rejected_before_provider_call(self):
        with patch.object(user_auth, "provider") as provider:
            response = self.client.post("/api/auth/accept-invite", headers=HEADERS, json={
                "token_hash": user_auth.sign_invite("test-verified-hash") + "x", "password": "test-password",
            })
        self.assertEqual(response.status_code, 400)
        provider.assert_not_called()

    def test_invite_rejects_non_application_token_before_password_update(self):
        with patch.object(user_auth, "provider", return_value={"user": {"id": OWNER}}) as provider:
            response = self.client.post("/api/auth/accept-invite", headers=HEADERS,
                json={"token_hash": user_auth.sign_invite("test-verified-hash"), "password": "test-password"})
        self.assertEqual(response.status_code, 403)
        provider.assert_awaited_once()

    def test_invitee_sets_password_and_must_complete_mfa(self):
        data, _ = admin_sessions.create(config.ADMIN_USERNAME, config.ADMIN_PASSWORD)
        data["refresh_token"] = "test-provider-invite-refresh"
        data["user"] = {"id": OWNER, "app_metadata": {"qa_access": {"invited": True, "permissions": {"bases.add": False}}}}
        with patch.object(user_auth, "provider", side_effect=[data, {}, data]) as provider:
            response = self.client.post("/api/auth/accept-invite", headers=HEADERS,
                json={"token_hash": user_auth.sign_invite("test-verified-hash"), "password": "test-password"})
            self.assertEqual(provider.await_args.args, ("PUT", "/user"))
            self.assertEqual(response.json()["mfa_required"], "setup")
            self.assertNotIn(user_auth.REFRESH, self.client.cookies)
            response = complete_mfa(self.client, response, OWNER, HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["user"]["permissions"]["bases.add"])
        self.assertIn(user_auth.REFRESH, self.client.cookies)

    def test_purge_cleans_only_target_paths_and_vector_collections(self):
        from rag import cloud, vectorstore
        target_suffix = "user_" + OWNER.replace("-", "")
        other_suffix = "user_" + OTHER.replace("-", "")
        namespace = "api_0123456789abcdef_" + target_suffix
        folder = self.root / "data" / "workspaces"
        for name in (target_suffix, other_suffix):
            (folder / name).mkdir(parents=True)
            (folder / name / "registry.json").write_text("{}")
        client = Mock()
        client.get_collections.return_value.collections = [
            SimpleNamespace(name=namespace + "_custom_base"), SimpleNamespace(name=other_suffix + "_custom_base"),
        ]
        document_path = namespace + "/custom_base/file.pdf"
        with patch.object(cloud, "rows", side_effect=[
            [{"namespace": namespace, "object_path": document_path}],
            [{"id": OTHER, "object_path": OWNER + "/" + OTHER}],
        ]), patch.object(cloud, "_request", return_value=Mock(json=lambda: [])) as storage, patch.object(cloud, "remove") as remove, patch.object(
            vectorstore, "get_client", return_value=client
        ):
            super_admin.purge_workspace(OWNER)
        self.assertFalse((folder / target_suffix).exists())
        self.assertTrue((folder / other_suffix).exists())
        client.delete_collection.assert_called_once_with(namespace + "_custom_base")
        self.assertEqual(storage.call_count, 3)
        remove.assert_any_call("qa_documents", namespace="eq." + namespace)
        remove.assert_any_call("qa_domains", owner_id="eq." + OWNER)
