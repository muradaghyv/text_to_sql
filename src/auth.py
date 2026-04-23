import os

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer    = HTTPBearer()
_ALGORITHM = "HS256"


def _decode(token: str) -> dict:
    secret = os.getenv("JWT_SECRET_KEY", "")
    if not secret:
        raise HTTPException(status_code=500, detail="JWT_SECRET_KEY not configured")
    try:
        return jwt.decode(token, secret, algorithms=[_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")


async def get_current_emp_id(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> int:
    payload = _decode(credentials.credentials)
    emp_id = payload.get("emp_id")
    if emp_id is None:
        raise HTTPException(status_code=401, detail="Token missing 'emp_id' claim")
    return int(emp_id)
