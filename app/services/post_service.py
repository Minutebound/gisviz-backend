from sqlalchemy.orm import Session
from sqlalchemy import text
from app.db.models import Publication, User
import json

class GeographicPublicationService:
    def retrieve_global_stream(self, spatial_db: Session, auth_db: Session, skip: int = 0, limit: int = 50):
        # 1. Fetch raw spatial data from PostGIS using the NEW schema names
        query = text("""
            SELECT 
                id, author_user_id, parent_publication_id, 
                title, description, primary_airport_geocode,
                layer_metadata, tags, 
                view_count, likes_count, comments_count, saves_count, 
                is_public, created_at, updated_at,
                data_license, temporal_start, temporal_end,
                ST_AsGeoJSON(geometry) as geometry,
                ST_AsGeoJSON(bounding_box) as bounding_box
            FROM publications
            ORDER BY created_at DESC
            OFFSET :skip LIMIT :limit
        """)
        
        raw_posts = spatial_db.execute(query, {"skip": skip, "limit": limit}).fetchall()
        
        if not raw_posts:
            return []
            
        # 2. Extract unique author IDs to look up in the Auth DB
        author_ids = list(set([str(row.author_user_id) for row in raw_posts]))
        
        # 3. Fetch user profiles from Auth DB using the new `User` model properties
        users = auth_db.query(User).filter(User.id.in_(author_ids)).all()
        user_map = {str(u.id): u for u in users}
        
        # 4. Stitch the spatial data and user profiles together
        formatted_posts = []
        for row in raw_posts:
            post_dict = dict(row._mapping)
            
            # Parse PostGIS GeoJSON strings back into Python dictionaries
            if post_dict.get('geometry'):
                post_dict['geometry'] = json.loads(post_dict['geometry'])
            if post_dict.get('bounding_box'):
                post_dict['bounding_box'] = json.loads(post_dict['bounding_box'])
            
            # Attach User metadata mapping to the new columns
            author = user_map.get(str(post_dict['author_user_id']))
            post_dict['author_handle'] = author.handle if author else "Unknown User"
            post_dict['author_avatar_url'] = author.avatar_url if author else ""
            
            formatted_posts.append(post_dict)
            
        return formatted_posts

post_service = GeographicPublicationService()