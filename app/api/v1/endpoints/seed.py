from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from passlib.context import CryptContext
from datetime import datetime, timedelta, timezone
import uuid

from app.db.database import get_auth_db, get_spatial_db
from app.db.models import User, UserFollow, Publication, Comment, Like, Bookmark

router = APIRouter()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

@router.post("/seed", status_code=201)
def seed_database(
    auth_db: Session = Depends(get_auth_db),
    spatial_db: Session = Depends(get_spatial_db)
):
    # Safety Check: Prevent accidental overwriting in production
    if auth_db.query(User).first():
        return {"message": "Database is already seeded. Aborting to protect existing data."}

    try:
        # ==========================================
        # 1. SEED AUTHENTICATION DATABASE
        # ==========================================
        hashed_pw = pwd_context.hash("SecurePassword2026!")
        now = datetime.now(timezone.utc)

        u1_id = uuid.uuid4()
        u2_id = uuid.uuid4()
        u3_id = uuid.uuid4()

        # Seed Users with new Engagement & Analytics fields
        users = [
            User(
                id=u1_id, handle="sujith_dev", email="sujith@gisviz.com", 
                hashed_password=hashed_pw, bio="Building the ultimate emerald-themed spatial network.", 
                is_verified=True, role="admin", subscription_tier="enterprise", auth_provider="local",
                preferences={"theme": "dark", "default_projection": "EPSG:4326", "accent": "emerald"},
                follower_count=2, following_count=0, total_posts_count=1,
                total_received_likes=17, total_received_saves=2, last_active_at=now
            ),
            User(
                id=u2_id, handle="sarah_geo", email="sarah@gisviz.com", 
                hashed_password=hashed_pw, bio="Mapping the world one coordinate at a time.", 
                is_verified=True, role="creator", subscription_tier="pro", auth_provider="google",
                preferences={"theme": "system", "default_projection": "EPSG:3857"},
                follower_count=0, following_count=1, total_posts_count=2,
                total_received_likes=29, total_received_saves=2, last_active_at=now - timedelta(hours=2)
            ),
            User(
                id=u3_id, handle="david_c", email="david@gisviz.com", 
                hashed_password=hashed_pw, bio="Travel tech & affiliate routing architect.", 
                is_verified=False, role="user", subscription_tier="free", auth_provider="local",
                preferences={"theme": "light"},
                follower_count=0, following_count=1, total_posts_count=1,
                total_received_likes=8, total_received_saves=1, last_active_at=now - timedelta(days=1)
            )
        ]
        auth_db.add_all(users)
        
        # Create Social Graph
        follows = [
            UserFollow(follower_id=u2_id, following_id=u1_id),
            UserFollow(follower_id=u3_id, following_id=u1_id)
        ]
        auth_db.add_all(follows)
        auth_db.commit()

        # ==========================================
        # 2. SEED SPATIAL DATABASE
        # ==========================================
        pub1_id = uuid.uuid4()
        pub2_id = uuid.uuid4()
        pub3_id = uuid.uuid4()
        pub4_id = uuid.uuid4() 

        # Seed Publications with airport geocodes and new analytics params
        publications = [
            Publication(
                id=pub1_id,
                author_user_id=u1_id,
                title="Boulder Electric Scooter Heatmap",
                description="Spatial distribution of EV scooter hubs relative to the Class of 2026 Grad Bash locations.",
                primary_airport_geocode="DEN", 
                geometry="SRID=4326;POINT(-104.6737 39.8561)", 
                bounding_box="SRID=4326;POLYGON((-104.8 39.7, -104.5 39.7, -104.5 40.0, -104.8 40.0, -104.8 39.7))",
                temporal_start=now - timedelta(days=30),
                temporal_end=now,
                data_license="Open Data Commons (ODC-By)",
                tags=["emerald", "transportation", "micro-mobility"],
                layer_metadata={"base": "emerald-dark", "zoom": 12},
                view_count=1452, likes_count=12, comments_count=1, saves_count=2, shares_count=45, engagement_rate=4
            ),
            Publication(
                id=pub2_id,
                author_user_id=u3_id,
                title="Amadeus API Flight Routing Nodes",
                description="Visualizing direct booking order management flow versus affiliate network clustering.",
                primary_airport_geocode="LHR", 
                geometry="SRID=4326;POINT(-0.4542 51.4700)",
                bounding_box="SRID=4326;POLYGON((-0.6 51.3, -0.3 51.3, -0.3 51.6, -0.6 51.6, -0.6 51.3))",
                data_license="Proprietary Enterprise",
                tags=["travel-tech", "aviation", "api"],
                layer_metadata={"base": "technical", "zoom": 5},
                view_count=890, likes_count=8, comments_count=0, saves_count=1, shares_count=12, engagement_rate=2
            ),
            Publication(
                id=pub3_id,
                author_user_id=u2_id,
                title="AI Medical Diagnostic Centers",
                description="Predictive mapping of diagnostic clinics for software-as-a-medical-device deployment.",
                primary_airport_geocode="SFO",
                geometry="SRID=4326;POINT(-122.3789 37.6188)",
                bounding_box="SRID=4326;POLYGON((-122.5 37.5, -122.1 37.5, -122.1 37.8, -122.5 37.8, -122.5 37.5))",
                temporal_start=now,
                temporal_end=now + timedelta(days=365),
                data_license="Standard Network License",
                tags=["healthcare", "ai", "diagnostics"],
                layer_metadata={"base": "clinical", "zoom": 9},
                view_count=3204, likes_count=24, comments_count=1, saves_count=1, shares_count=115, engagement_rate=4
            ),
            Publication(
                id=pub4_id,
                author_user_id=u2_id, 
                parent_publication_id=pub1_id, 
                title="Boulder Scooter Heatmap (Topographic Remix)",
                description="Forked the original network to overlay elevation contours for battery drain prediction.",
                primary_airport_geocode="DEN", 
                geometry="SRID=4326;POINT(-104.6737 39.8561)", 
                bounding_box="SRID=4326;POLYGON((-104.8 39.7, -104.5 39.7, -104.5 40.0, -104.8 40.0, -104.8 39.7))",
                data_license="Open Data Commons (ODC-By)",
                tags=["elevation", "micro-mobility", "remix"],
                layer_metadata={"base": "terrain-light", "zoom": 13, "elevation_contours": True},
                view_count=412, likes_count=5, comments_count=0, saves_count=1, shares_count=3, engagement_rate=2
            )
        ]
        spatial_db.add_all(publications)

        # Seed Engagement (Likes & Comments)
        likes = [
            Like(publication_id=pub1_id, user_id=u2_id),
            Like(publication_id=pub3_id, user_id=u1_id),
            Like(publication_id=pub4_id, user_id=u1_id) 
        ]
        
        comments = [
            Comment(publication_id=pub1_id, author_user_id=u2_id, content="The emerald theme makes this data pop perfectly."),
            Comment(publication_id=pub3_id, author_user_id=u1_id, content="Great use of the airport geocodes for the regional anchors.")
        ]
        
        # Seed Bookmarks (Saves)
        bookmarks = [
            Bookmark(publication_id=pub1_id, user_id=u3_id),
            Bookmark(publication_id=pub1_id, user_id=u2_id),
            Bookmark(publication_id=pub2_id, user_id=u1_id),
            Bookmark(publication_id=pub3_id, user_id=u3_id),
            Bookmark(publication_id=pub4_id, user_id=u3_id)
        ]
        
        spatial_db.add_all(likes)
        spatial_db.add_all(comments)
        spatial_db.add_all(bookmarks)
        spatial_db.commit()

        return {
            "status": "success",
            "message": "Enterprise Auth and Spatial databases have been successfully seeded with analytics tracking.",
            "data": {
                "users_created": len(users),
                "publications_created": len(publications),
                "engagement_records": len(likes) + len(comments) + len(bookmarks)
            }
        }

    except Exception as e:
        auth_db.rollback()
        spatial_db.rollback()
        raise HTTPException(status_code=500, detail=f"Database seeding failed: {str(e)}")