import asyncpg
import os
from datetime import datetime
from typing import List, Dict, Optional
import logging

logger = logging.getLogger(__name__)

class Database:
    def __init__(self):
        self.pool = None
        self.database_url = os.getenv("DATABASE_URL")
        
    async def connect(self):
        """Create database connection pool"""
        try:
            self.pool = await asyncpg.create_pool(
                self.database_url,
                min_size=5,
                max_size=20,
                command_timeout=60
            )
            
            # Create tables if they don't exist
            await self.create_tables()
            logger.info("Database connected successfully")
        except Exception as e:
            logger.error(f"Database connection failed: {e}")
            raise
    
    async def create_tables(self):
        """Create necessary tables"""
        async with self.pool.acquire() as conn:
            # Users table
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username VARCHAR(255),
                    first_name VARCHAR(255),
                    last_name VARCHAR(255),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Projects table
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS projects (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    name VARCHAR(500) NOT NULL,
                    url TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Sessions/Conversations table
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS user_sessions (
                    user_id BIGINT PRIMARY KEY,
                    state VARCHAR(50),
                    data JSONB,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Pomodoro sessions table
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS pomodoro_sessions (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    start_time TIMESTAMP,
                    end_time TIMESTAMP,
                    duration INTEGER,
                    completed BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Create indexes for better performance
            await conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_projects_user_id ON projects(user_id);
                CREATE INDEX IF NOT EXISTS idx_pomodoro_user_id ON pomodoro_sessions(user_id);
                CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
            ''')
    
    async def get_or_create_user(self, user_id: int, username: str = None, 
                                   first_name: str = None, last_name: str = None) -> Dict:
        """Get user or create if doesn't exist"""
        async with self.pool.acquire() as conn:
            # Try to get existing user
            row = await conn.fetchrow(
                'SELECT * FROM users WHERE user_id = $1',
                user_id
            )
            
            if row:
                # Update last_active
                await conn.execute(
                    'UPDATE users SET last_active = CURRENT_TIMESTAMP WHERE user_id = $1',
                    user_id
                )
                return dict(row)
            else:
                # Create new user
                await conn.execute('''
                    INSERT INTO users (user_id, username, first_name, last_name)
                    VALUES ($1, $2, $3, $4)
                ''', user_id, username, first_name, last_name)
                
                row = await conn.fetchrow(
                    'SELECT * FROM users WHERE user_id = $1',
                    user_id
                )
                return dict(row)
    
    async def add_project(self, user_id: int, name: str, url: str) -> bool:
        """Add a new project for user"""
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO projects (user_id, name, url)
                    VALUES ($1, $2, $3)
                ''', user_id, name, url)
                return True
        except Exception as e:
            logger.error(f"Error adding project: {e}")
            return False
    
    async def get_projects(self, user_id: int) -> List[Dict]:
        """Get all projects for a user"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT * FROM projects WHERE user_id = $1 ORDER BY created_at DESC',
                user_id
            )
            return [dict(row) for row in rows]
    
    async def update_project(self, project_id: int, user_id: int, name: str = None, url: str = None) -> bool:
        """Update a project"""
        try:
            async with self.pool.acquire() as conn:
                if name and url:
                    await conn.execute('''
                        UPDATE projects 
                        SET name = $1, url = $2, updated_at = CURRENT_TIMESTAMP
                        WHERE id = $3 AND user_id = $4
                    ''', name, url, project_id, user_id)
                elif name:
                    await conn.execute('''
                        UPDATE projects 
                        SET name = $1, updated_at = CURRENT_TIMESTAMP
                        WHERE id = $2 AND user_id = $3
                    ''', name, project_id, user_id)
                elif url:
                    await conn.execute('''
                        UPDATE projects 
                        SET url = $1, updated_at = CURRENT_TIMESTAMP
                        WHERE id = $2 AND user_id = $3
                    ''', url, project_id, user_id)
                return True
        except Exception as e:
            logger.error(f"Error updating project: {e}")
            return False
    
    async def delete_project(self, project_id: int, user_id: int) -> bool:
        """Delete a project"""
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    'DELETE FROM projects WHERE id = $1 AND user_id = $2',
                    project_id, user_id
                )
                return True
        except Exception as e:
            logger.error(f"Error deleting project: {e}")
            return False
    
    async def save_session(self, user_id: int, state: str, data: dict):
        """Save conversation state"""
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO user_sessions (user_id, state, data, updated_at)
                VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id) 
                DO UPDATE SET state = $2, data = $3, updated_at = CURRENT_TIMESTAMP
            ''', user_id, state, data)
    
    async def get_session(self, user_id: int):
        """Get conversation state"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT state, data FROM user_sessions WHERE user_id = $1',
                user_id
            )
            if row:
                return row['state'], row['data']
            return None, None
    
    async def clear_session(self, user_id: int):
        """Clear conversation state"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'DELETE FROM user_sessions WHERE user_id = $1',
                user_id
            )
    
    async def add_pomodoro_session(self, user_id: int, start_time, end_time, duration, completed=False):
        """Log Pomodoro session"""
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO pomodoro_sessions (user_id, start_time, end_time, duration, completed)
                VALUES ($1, $2, $3, $4, $5)
            ''', user_id, start_time, end_time, duration, completed)
    
    async def close(self):
        """Close database connection pool"""
        if self.pool:
            await self.pool.close()

# Global database instance
db = Database()