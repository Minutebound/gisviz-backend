from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from typing import List

from app.db.database import get_posts_db, get_users_db
from app.db.models import PlatformUserRecord
from app.schemas.post_schema import (
    GeographicPublicationResponse,
    GeographicPublicationPayload,
    LikeResponse,
    CommentPayload,
    CommentData,
)
from app.services.post_service import post_service
from app.services.cache_service import cache_service
from app.services.auth_service import get_current_authenticated_user

router = APIRouter()


@router.get("/stream", response_model=List[GeographicPublicationResponse])
def fetch_spatial_publication_stream(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
):
    return post_service.retrieve_global_stream(
        posts_db=posts_db, users_db=users_db, skip=skip, limit=limit
    )


@router.get("/search", response_model=List[GeographicPublicationResponse])
def search_publication_stream(
    q: str = Query(..., min_length=1),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
):
    return post_service.search_publications(
        posts_db=posts_db, users_db=users_db, query=q, skip=skip, limit=limit
    )


@router.get("/trending", response_model=List[str])
def trending_publications(n: int = Query(10, ge=1, le=50)):
    return cache_service.top_trending(n)


@router.post("", response_model=GeographicPublicationResponse, status_code=201)
def create_publication(
    payload: GeographicPublicationPayload,
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    return post_service.create_publication(
        posts_db=posts_db,
        users_db=users_db,
        author_user_id=str(current_user.user_id),
        title=payload.publication_title,
        geojson=payload.spatial_geometry_geojson,
        metadata=payload.layer_attribute_metadata,
        category_ids=payload.category_ids,
    )


@router.post("/{publication_id}/like", response_model=LikeResponse)
def toggle_like(
    publication_id: str,
    posts_db: Session = Depends(get_posts_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    try:
        return post_service.toggle_like(posts_db, publication_id, str(current_user.user_id))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{publication_id}/comments", response_model=CommentData, status_code=201)
def add_comment(
    publication_id: str,
    payload: CommentPayload,
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
    current_user: PlatformUserRecord = Depends(get_current_authenticated_user),
):
    try:
        comment = post_service.add_comment(
            posts_db,
            publication_id,
            str(current_user.user_id),
            payload.content,
            str(payload.parent_comment_id) if payload.parent_comment_id else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return CommentData(
        comment_id=comment.comment_id,
        publication_id=comment.publication_id,
        user_id=comment.user_id,
        author_handle=current_user.user_handle,
        author_avatar_url=current_user.avatar_storage_url,
        parent_comment_id=comment.parent_comment_id,
        content=comment.content,
        is_edited=bool(comment.is_edited),
        created_timestamp=comment.created_timestamp,
        updated_timestamp=comment.updated_timestamp,
        replies=[],
    )


@router.get("/{publication_id}/comments", response_model=List[CommentData])
def get_comments(
    publication_id: str,
    posts_db: Session = Depends(get_posts_db),
    users_db: Session = Depends(get_users_db),
):
    return post_service.get_comment_thread(posts_db, users_db, publication_id)