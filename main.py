import os
from contextlib import asynccontextmanager
from typing import List, Optional

from alembic import command
from alembic.config import Config
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from database import get_db
from models import CategoryModel, RecordModel, UserModel
from schemas import (
    MAX_ID,
    CategoryCreate,
    CategoryOut,
    RecordCreate,
    RecordOut,
    UserCreate,
    UserOut,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def run_migrations():
    # автоматично застосовуємо всі міграції Alembic до останньої версії
    config = Config(os.path.join(BASE_DIR, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(BASE_DIR, "alembic"))
    command.upgrade(config, "head")


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_migrations()
    yield


app = FastAPI(title="Expense API", lifespan=lifespan)


class ApiError(Exception):
    def __init__(self, status_code: int, error: str, message: str):
        self.status_code = status_code
        self.error = error
        self.message = message


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.error, "message": exc.message})


# Некоректні дані (тіло, query, path, невалідний JSON) -> 400 замість стандартного 422
@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    parts = []
    for err in exc.errors():
        field = ".".join(str(x) for x in err["loc"] if x not in ("body", "query", "path"))
        parts.append(f"{field}: {err['msg']}" if field else err["msg"])
    return JSONResponse(status_code=400, content={"error": "validation_error", "message": "; ".join(parts)})


def get_or_404(db: Session, model, obj_id: int, name: str):
    obj = db.get(model, obj_id) if 0 < obj_id <= MAX_ID else None
    if obj is None:
        raise ApiError(404, "not_found", f"{name} with id {obj_id} not found")
    return obj


def check_category_access(category: CategoryModel, user_id: Optional[int]):
    # персональна категорія доступна лише її власнику, глобальна — всім
    if category.user_id is not None and category.user_id != user_id:
        raise ApiError(
            403, "forbidden", f"Category with id {category.id} is a personal category of another user"
        )


# Users

@app.get("/users", response_model=List[UserOut])
def get_users(db: Session = Depends(get_db)):
    return db.scalars(select(UserModel).order_by(UserModel.id)).all()


@app.get("/user/{user_id}", response_model=UserOut)
def get_user(user_id: int, db: Session = Depends(get_db)):
    return get_or_404(db, UserModel, user_id, "User")


@app.post("/user", response_model=UserOut, status_code=201)
def create_user(data: UserCreate, db: Session = Depends(get_db)):
    user = UserModel(name=data.name)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@app.delete("/user/{user_id}", status_code=204)
def delete_user(user_id: int, db: Session = Depends(get_db)):
    user = get_or_404(db, UserModel, user_id, "User")
    # записи та персональні категорії видаляє БД (ON DELETE CASCADE)
    db.delete(user)
    db.commit()
    return Response(status_code=204)


# Categories

@app.get("/category", response_model=List[CategoryOut])
def get_categories(user_id: Optional[int] = None, db: Session = Depends(get_db)):
    # без user_id — лише глобальні категорії,
    # з user_id — глобальні + персональні категорії цього користувача
    query = select(CategoryModel).order_by(CategoryModel.id)
    if user_id is None:
        query = query.where(CategoryModel.user_id.is_(None))
    else:
        get_or_404(db, UserModel, user_id, "User")
        query = query.where(or_(CategoryModel.user_id.is_(None), CategoryModel.user_id == user_id))
    return db.scalars(query).all()


@app.get("/category/{category_id}", response_model=CategoryOut)
def get_category(category_id: int, user_id: Optional[int] = None, db: Session = Depends(get_db)):
    category = get_or_404(db, CategoryModel, category_id, "Category")
    check_category_access(category, user_id)
    return category


@app.post("/category", response_model=CategoryOut, status_code=201)
def create_category(data: CategoryCreate, db: Session = Depends(get_db)):
    if data.user_id is not None:
        get_or_404(db, UserModel, data.user_id, "User")
    category = CategoryModel(name=data.name, user_id=data.user_id)
    db.add(category)
    db.commit()
    db.refresh(category)
    return category


@app.delete("/category/{category_id}", status_code=204)
def delete_category(category_id: int, db: Session = Depends(get_db)):
    category = get_or_404(db, CategoryModel, category_id, "Category")
    db.delete(category)
    db.commit()
    return Response(status_code=204)


# Records

@app.post("/record", response_model=RecordOut, status_code=201)
def create_record(data: RecordCreate, db: Session = Depends(get_db)):
    get_or_404(db, UserModel, data.user_id, "User")
    category = get_or_404(db, CategoryModel, data.category_id, "Category")
    check_category_access(category, data.user_id)
    # created_at не передано — БД підставить поточний час
    record = RecordModel(**data.model_dump(exclude_none=True))
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@app.get("/record/{record_id}", response_model=RecordOut)
def get_record(record_id: int, db: Session = Depends(get_db)):
    return get_or_404(db, RecordModel, record_id, "Record")


@app.delete("/record/{record_id}", status_code=204)
def delete_record(record_id: int, db: Session = Depends(get_db)):
    record = get_or_404(db, RecordModel, record_id, "Record")
    db.delete(record)
    db.commit()
    return Response(status_code=204)


@app.get("/record", response_model=List[RecordOut])
def get_records(
    user_id: Optional[int] = None,
    category_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    if user_id is None and category_id is None:
        raise ApiError(
            400,
            "invalid_request",
            "At least one query parameter ('user_id' or 'category_id') is required for filtration",
        )
    query = select(RecordModel).order_by(RecordModel.id)
    if user_id is not None:
        query = query.where(RecordModel.user_id == user_id)
    if category_id is not None:
        query = query.where(RecordModel.category_id == category_id)
    return db.scalars(query).all()
