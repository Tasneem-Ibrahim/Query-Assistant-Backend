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
            self.assertEqual(self.client.get("/api/domains/example/documents/test/pdf", headers=headers).status_code, 403)
            self.assertEqual(self.client.post("/api/domains/example/ingest", headers=headers).status_code, 400)
            self.assertEqual(self.client.post("/api/domains/example/reset", headers=headers).status_code, 403)
            with patch.object(api_server.extract, "document_pdf"), patch.object(api_server.ingest, "ingest_pdf", side_effect=ingest_selected):
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
            super_admin.validate_permissions("user", {"approvals.edit": True})

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
        security_store.write("account:local-admin", {"approvals.add": True, "approvals.delete": True}, 0)

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
        self.assertEqual(self.client.put(endpoint + "/admin", headers=HEADERS, json={**policy, "modules": {"roles": True}}).status_code, 400)
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
        with patch("rag.revisions.cloud._request") as rpc:
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
        self.assertFalse(access_control.available("admin", "roles.edit"))
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
                         {"role": "user", "permissions": {"bases.add": False}})

        with patch("rag.super_admin.cloud._request"), patch.object(
            super_admin, "auth", return_value={**user, "hashed_token": "test-invite-token"},
        ) as auth, patch.object(
            super_admin, "update", return_value=user,
        ) as update:
            result = self.client.post("/api/super-admin/accounts", headers=HEADERS, json={**data, "method": "invite"})
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.json()["invite_token"], user_auth.sign_invite("test-invite-token"))
        self.assertEqual(update.call_args.kwargs, {"role": "user", "invited": True, "permissions": {"bases.add": False}})
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
