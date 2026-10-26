import argparse

from firebase_admin import auth

from app.auth import get_firebase_app


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Assign a trusted ThibitiFare role to an existing Firebase user."
    )
    parser.add_argument("--email", required=True, help="Email of an existing Firebase user")
    parser.add_argument(
        "--role",
        required=True,
        choices=("admin", "conductor"),
        help="Role to assign",
    )
    arguments = parser.parse_args()

    app = get_firebase_app()
    user = auth.get_user_by_email(arguments.email, app=app)
    claims = dict(user.custom_claims or {})
    claims["role"] = arguments.role
    auth.set_custom_user_claims(user.uid, claims, app=app)
    auth.revoke_refresh_tokens(user.uid, app=app)
    print(f"Assigned role '{arguments.role}' to Firebase UID {user.uid}.")
    print("The user must sign out and sign in again to receive the updated role.")


if __name__ == "__main__":
    main()
