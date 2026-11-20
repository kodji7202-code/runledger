"""Team-scoped SCIM resources. User email and name are server-wide attributes."""
import re
from urllib.parse import quote

from .auth import InvalidKey

CORE = "urn:ietf:params:scim:schemas:core:2.0:"
MESSAGES = "urn:ietf:params:scim:api:messages:2.0:"
ROLES = ("admin", "member", "viewer")


class Error(Exception):
    def __init__(self, status, detail, kind=None):
        self.status, self.detail, self.kind = status, detail, kind


def send(h, status, body, headers=None):
    from .app import _json_bytes
    h._send(status, b"" if status == 204 else _json_bytes(body), "application/scim+json; charset=utf-8", headers)


def listing(resources, start=1, count=200):
    return {"schemas": [MESSAGES + "ListResponse"], "totalResults": len(resources),
            "startIndex": start, "itemsPerPage": len(resources[start - 1:start - 1 + count]),
            "Resources": resources[start - 1:start - 1 + count]}


def paginated(resources, query):
    try:
        start, count = int(query.get("startIndex", ["1"])[0]), int(query.get("count", ["200"])[0])
    except ValueError:
        raise Error(400, "Invalid pagination.", "invalidValue")
    return listing(resources, max(1, start), min(200, max(0, count)))


def resource(member):
    location = "/scim/v2/Users/" + quote(member["id"])
    return {"schemas": [CORE + "User"], "id": member["id"], "userName": member["email"],
            "name": {"formatted": member["name"] or ""}, "displayName": member["name"] or "",
            "emails": [{"value": member["email"], "primary": True}],
            "active": member["deactivated_at"] is None, "externalId": member["external_id"],
            "roles": [{"value": member["role"]}],
            "meta": {"resourceType": "User", "created": member["joined_at"],
                     "lastModified": member["updated_at"] or member["joined_at"], "location": location}}


def name_from_parts(parts):
    names = [parts.get(key) for key in ("givenName", "familyName")]
    if any(name is not None and not isinstance(name, str) for name in names):
        raise Error(400, "Name parts must be strings.", "invalidValue")
    return " ".join((name or "").strip() for name in names).strip() or None


def values(body):
    result = {}
    for key, value in body.items():
        key = key.lower()
        if key == "active":
            if isinstance(value, str) and value.lower() in ("true", "false"):
                value = value.lower() == "true"
            if not isinstance(value, bool):
                raise Error(400, "active must be a boolean.", "invalidValue")
            result["active"] = value
        elif key == "username":
            result["email"] = value
        elif key in ("displayname", "name.formatted"):
            result["name"] = value
        elif key == "name":
            if not isinstance(value, dict):
                raise Error(400, "name must be an object.", "invalidValue")
            result["name"] = value["formatted"] if "formatted" in value else name_from_parts(value)
        elif key == "externalid":
            if value is not None and not isinstance(value, str):
                raise Error(400, "externalId must be a string.", "invalidValue")
            result["external_id"] = value
        elif key == "roles":
            if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict) or value[0].get("value") not in ROLES:
                raise Error(400, "Supply one role: admin, member or viewer.", "invalidValue")
            result["role"] = value[0]["value"]
    if isinstance(result.get("name"), str) and not result["name"].strip():
        result["name"] = None
    return result


def operations(body):
    ops = body.get("Operations")
    if not isinstance(ops, list) or not ops or any(not isinstance(op, dict) for op in ops):
        raise Error(400, "Operations must be a nonempty array.", "invalidSyntax")
    for op in ops:
        if not isinstance(op.get("op"), str) or op["op"].lower() not in ("add", "replace", "remove"):
            raise Error(400, "Unsupported patch operation.", "invalidSyntax")
    return ops


def dispatch(h, method, path, query):
    from .app import _HttpError, _json_object
    db = h.server.db
    if path == "/api/scim/token":
        h._only(method, ("GET", "POST", "DELETE"))
        ident = h._authed("admin", "manage SCIM tokens", write=method != "GET")
        result = db.scim_token(ident.team_id, {"GET": "get", "POST": "create", "DELETE": "revoke"}[method], ident)
        h._send_json(201 if method == "POST" else 200, result)
        return
    try:
        token = h._bearer_token()
        team = db.scim_team(token)
        if team is None:
            if token is not None:
                h.server.limiter.record_failure(h._client_ip())
            raise Error(401, "A valid SCIM bearer token is required.")
        parts = path[len("/scim/v2/"):].strip("/").split("/")
        kind = parts[0]
        rid = parts[1] if len(parts) == 2 else None
        if len(parts) > 2:
            raise Error(404, "Resource not found.")
        body = {}
        if method in ("POST", "PUT", "PATCH"):
            length = h._content_length(65536, "SCIM requests")
            content_type = h.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if content_type not in ("application/json", "application/scim+json"):
                raise Error(415, "Send application/scim+json or application/json.")
            body = _json_object(h.rfile.read(length))
        if kind in ("ServiceProviderConfig", "Schemas", "ResourceTypes"):
            if method != "GET":
                raise Error(405, "Discovery resources are read-only.")
            if kind == "ServiceProviderConfig":
                result = {"schemas": [CORE + kind], "patch": {"supported": True},
                          "filter": {"supported": True, "maxResults": 200},
                          "authenticationSchemes": [{"type": "oauthbearertoken", "name": "Bearer token", "description": "Per-team SCIM token"}]}
                for feature in ("bulk", "sort", "etag", "changePassword"):
                    result[feature] = {"supported": False}
            elif kind == "Schemas":
                result = listing([{"id": CORE + name, "name": name, "schemas": [CORE + "Schema"], "attributes": []} for name in ("User", "Group")])
            else:
                result = listing([{"schemas": [CORE + "ResourceType"], "id": name, "name": name, "endpoint": "/" + name + "s", "schema": CORE + name} for name in ("User", "Group")])
            send(h, 200, result)
            return
        members = db.list_members(team)
        member = next((m for m in members if m["id"] == rid), None)
        if kind == "Groups":
            groups(h, db, team, method, rid, body, members, query)
            return
        if kind != "Users":
            raise Error(404, "Resource not found.")
        if rid and member is None:
            raise Error(404, "User not found.")
        if method == "GET":
            if rid:
                result = resource(member)
            else:
                if "filter" in query:
                    match = re.fullmatch(r'\s*(userName|externalId)\s+eq\s+"([^"\\]*)"\s*', query["filter"][0], re.I)
                    if not match:
                        raise Error(400, "Unsupported filter.", "invalidFilter")
                    key = "email" if match[1].lower() == "username" else "external_id"
                    members = [m for m in members if
                               (m[key].lower() == match[2].lower() if key == "email" else m[key] == match[2])]
                result = paginated([resource(m) for m in members], query)
            send(h, 200, result)
            return
        if method == "DELETE" and rid:
            db.change_member(team, rid, actor="scim")
            db.record_audit(team, "scim", "scim.user.delete", member["email"],
                            {"user_id": rid, "email": member["email"], "role": member["role"]})
            send(h, 204, {})
            return
        if method == "POST" and not rid:
            data = values(body)
            email = data.get("email")
            if isinstance(email, str) and any(m["email"].lower() == email.lower() for m in members):
                raise Error(409, "User is already a member.", "uniqueness")
            new = db.add_member(team, email, data.get("role", "member"), data.get("name"), actor="scim", scim_data=data)
            rid = new["id"]
            send(h, 201, resource(next(m for m in db.list_members(team) if m["id"] == rid)))
            return
        if method in ("PATCH", "PUT") and rid:
            if method == "PUT":
                data = {"name": None, "active": True, "role": "member", "external_id": None}
                data.update(values(body))
                if "email" not in data:
                    raise Error(400, "userName is required.", "invalidValue")
            else:
                data = {}
                name_parts = {}
                for op in operations(body):
                    path = op.get("path")
                    if path is None:
                        if not isinstance(op.get("value"), dict) or op["op"].lower() == "remove":
                            raise Error(400, "A value object is required.", "invalidValue")
                        data.update(values(op["value"]))
                    else:
                        if not isinstance(path, str) or path.lower() not in ("active", "name", "name.formatted", "name.givenname", "name.familyname", "displayname", "username", "externalid", "roles"):
                            raise Error(400, "Unsupported patch path.", "invalidPath")
                        value = op.get("value")
                        if op["op"].lower() == "remove":
                            if path.lower() in ("active", "username"):
                                raise Error(400, "Cannot remove required attribute.", "mutability")
                            value = [{"value": "member"}] if path.lower() == "roles" else {} if path.lower() == "name" else None
                        if path.lower() in ("name.givenname", "name.familyname"):
                            name_parts["givenName" if path.lower() == "name.givenname" else "familyName"] = value
                            data["name"] = name_from_parts(name_parts)
                        else:
                            data.update(values({path: value}))
            db.scim_update(team, rid, data)
            send(h, 200, resource(next(m for m in db.list_members(team) if m["id"] == rid)))
            return
        raise Error(405, "Method not supported.")
    except InvalidKey as exc:
        detail = str(exc)
        error(h, Error(409 if "already" in detail else 400, detail,
                       "uniqueness" if "already" in detail else "mutability" if "last working admin" in detail or "belongs to other teams" in detail else "invalidValue"))
    except Error as exc:
        error(h, exc)
    except _HttpError as exc:
        error(h, Error(exc.status, exc.message))


def error(h, exc, headers=None):
    body = {"schemas": [MESSAGES + "Error"], "status": str(exc.status), "detail": exc.detail}
    if exc.kind:
        body["scimType"] = exc.kind
    h.close_connection = True
    send(h, exc.status, body, headers)


def groups(h, db, team, method, rid, body, members, query=None):
    query = query or {}
    if rid and rid not in ROLES:
        raise Error(404, "Group not found.")
    if method == "GET":
        resources = [{"schemas": [CORE + "Group"], "id": role, "displayName": role,
                      "members": [{"value": m["id"], "display": m["email"]} for m in members if m["role"] == role]} for role in ROLES]
        if not rid and "filter" in query:
            match = re.fullmatch(r'\s*displayName\s+eq\s+"([^"\\]*)"\s*', query["filter"][0], re.I)
            if not match:
                raise Error(400, "Unsupported filter.", "invalidFilter")
            resources = [g for g in resources if g["displayName"].lower() == match[1].lower()]
        send(h, 200, next(g for g in resources if g["id"] == rid) if rid else paginated(resources, query))
        return
    if method != "PATCH" or not rid:
        raise Error(405, "Only GET and PATCH of fixed role groups are supported.")
    changes = {}
    for op in operations(body):
        path = op.get("path", "members")
        match = re.fullmatch(r'members\[value eq "([^"\\]+)"\]', path) if isinstance(path, str) else None
        entries = [{"value": match[1]}] if match else op.get("value")
        if isinstance(entries, dict):
            entries = entries.get("members")
        if (path != "members" and not match) or not isinstance(entries, list):
            raise Error(400, "Supply members or members[value eq \"id\"].", "invalidPath")
        if op["op"].lower() == "replace" and not match:
            for member in members:
                if changes.get(member["id"], member["role"]) == rid:
                    changes[member["id"]] = "viewer" if rid == "member" else "member" if rid == "admin" else "viewer"
        for entry in entries:
            user_id = entry.get("value") if isinstance(entry, dict) else None
            member = next((m for m in members if m["id"] == user_id), None)
            if member is None:
                raise Error(400, "Member not found.", "invalidValue")
            role = changes.get(user_id, member["role"])
            if op["op"].lower() == "remove":
                if role == rid:
                    changes[user_id] = "viewer" if rid == "member" else "member"
            else:
                changes[user_id] = rid
    db.scim_group_update(team, rid, changes)
    groups(h, db, team, "GET", rid, {}, db.list_members(team))
