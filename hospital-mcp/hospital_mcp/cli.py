"""Command line: seeding, bootstrap and dev tokens.

    python -m hospital_mcp.cli seed [--admin-email you@gmail.com] [--platform-email you@gmail.com]
    python -m hospital_mcp.cli add-staff --hospital H001 --email you@gmail.com --role admin --subject ADM-002 --name "Your Name"
    python -m hospital_mcp.cli users
    python -m hospital_mcp.cli dev-token reception@dummyhospital.dev      (AUTH_MODE=dev only)

The first admin of a hospital is created here (or by a platform admin), never through MCP (plan s.5).
"""
import argparse
import sys
from datetime import datetime

from .authz import STAFF_ROLES
from .config import get_settings
from .db import get_db
from .identity import TokenVerifier


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hospital_mcp.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed", help="(re)create the two demo hospitals with synthetic data")
    s.add_argument("--admin-email", help="make this e-mail the Dummy Hospital admin (for Auth0 sign-in)")
    s.add_argument("--platform-email", help="make this e-mail the platform admin")
    a = sub.add_parser("add-staff", help="add a staff login to a hospital")
    a.add_argument("--hospital", required=True)
    a.add_argument("--email", required=True)
    a.add_argument("--role", required=True, choices=STAFF_ROLES + ("platform_admin",))
    a.add_argument("--subject", required=True, help="staff code, e.g. ADM-002 (doctors: their staff_id)")
    a.add_argument("--name", default="")
    a.add_argument("--department")
    sub.add_parser("users", help="list staff logins")
    t = sub.add_parser("dev-token", help="print a signed dev token for an e-mail (AUTH_MODE=dev)")
    t.add_argument("email")
    t.add_argument("--hours", type=int, default=8)
    args = ap.parse_args(argv)

    settings = get_settings()
    if args.cmd == "dev-token":
        print(TokenVerifier(settings).issue_dev_token(args.email, args.hours * 3600))
        return 0
    db = get_db()
    if args.cmd == "seed":
        from .seed import seed_all
        print(seed_all(db, admin_email=args.admin_email, platform_email=args.platform_email))
        print("Logins: python -m hospital_mcp.cli users")
    elif args.cmd == "add-staff":
        hid = None if args.role == "platform_admin" else args.hospital
        if hid and not db.hospitals.find_one({"hospital_id": hid}):
            print(f"No hospital {hid}", file=sys.stderr)
            return 1
        db.staff_users.insert_one({"hospital_id": hid, "subject": args.subject, "email": args.email.lower(),
                                   "name": args.name, "role": args.role, "department_id": args.department,
                                   "active": True, "auth_sub": None, "created_by": "cli",
                                   "created_at": datetime.now().replace(microsecond=0)})
        print(f"Added {args.email} as {args.role} at {hid or 'platform'}")
    elif args.cmd == "users":
        for u in db.staff_users.find().sort([("hospital_id", 1), ("role", 1)]):
            print(f"{u.get('hospital_id') or 'platform':9} {u['role']:15} {u['subject']:14} {u['email']:40} "
                  f"{'active' if u.get('active') else 'INACTIVE'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
