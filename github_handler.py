import httpx
import asyncio
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

class Config:
    GITHUB_API_URL = "https://api.github.com"
    REQUEST_TIMEOUT = 10.0
    MAX_RETRIES = 3

async def get_git_data(username):
    """Fetch GitHub user data with improved error handling"""
    async with httpx.AsyncClient(timeout=Config.REQUEST_TIMEOUT) as client:
        try:
            for attempt in range(Config.MAX_RETRIES):
                try:
                    r = await client.get(
                        f"{Config.GITHUB_API_URL}/users/{username}/repos",
                        params={"sort": "pushed", "per_page": 1}
                    )
                    
                    if r.status_code == 403:
                        reset_time = r.headers.get('X-RateLimit-Reset')
                        if reset_time:
                            reset_dt = datetime.fromtimestamp(int(reset_time))
                            wait_time = (reset_dt - datetime.now()).total_seconds()
                            if wait_time > 0:
                                return {"error": f"GitHub API rate limit exceeded. Resets at {reset_dt.strftime('%H:%M:%S')}"}
                        return {"error": "GitHub API rate limit exceeded. Please try again later."}
                    
                    if r.status_code == 404:
                        return {"error": f"User '{username}' not found"}
                    
                    r.raise_for_status()
                    repos = r.json()
                    
                    if not repos:
                        return {"error": f"No public repositories found for '{username}'"}
                    
                    repo_name = repos[0]['name']
                    
                    c = await client.get(
                        f"{Config.GITHUB_API_URL}/repos/{username}/{repo_name}/commits",
                        params={"per_page": 1}
                    )
                    
                    commits = c.json() if c.status_code == 200 else []
                    
                    return {
                        "repo": repo_name,
                        "msg": commits[0]['commit']['message'] if commits else "N/A",
                        "date": repos[0]['pushed_at'],
                        "link": repos[0]['html_url']
                    }
                    
                except httpx.TimeoutException:
                    if attempt == Config.MAX_RETRIES - 1:
                        return {"error": "Request timeout. Please try again."}
                    await asyncio.sleep(2 ** attempt)
                    
        except Exception as e:
            logger.error(f"GitHub API error: {e}")
            return {"error": f"API Error: {str(e)}"}