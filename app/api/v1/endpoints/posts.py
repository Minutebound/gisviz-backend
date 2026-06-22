from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import List

from app.db.database import get_spatial_db, get_auth_db
from app.schemas.post_schema import GeographicPublicationResponse
from app.services.post_service import post_service

router = APIRouter()

@router.get("/stream", response_model=List[GeographicPublicationResponse])
def fetch_spatial_publication_stream(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    spatial_db: Session = Depends(get_spatial_db), 
    auth_db: Session = Depends(get_auth_db),       
):
    # This endpoint is now completely public
    return post_service.retrieve_global_stream(
        spatial_db=spatial_db, 
        auth_db=auth_db, 
        skip=skip, 
        limit=limit
    )