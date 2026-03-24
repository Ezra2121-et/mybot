import os
import asyncio
import logging
import warnings
import httpx
from datetime import datetime, timezone
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, 
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters
)
from telegram.warnings import PTBUserWarning

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Suppress warnings
warnings.filterwarnings("ignore", category=PTBUserWarning)

# Load environment variables
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not TOKEN:
    raise ValueError("❌ TELEGRAM_TOKEN not found in .env file!")
if not DATABASE_URL:
    raise ValueError("❌ DATABASE_URL not found in .env file!")

# Database import
import asyncpg

# Database connection pool
db_pool = None

# Constants for State Machine
NAME, URL, CONFIRM, GET_GIT_USER, EDIT_PROJECT, EDIT_FIELD, DELETE_PROJECT_CONFIRM = range(7)

# Store active timers
active_timers = {}

def escape_markdown(text):
    """Escape special characters for Markdown"""
    special_chars = r'_*[]()~`>#+-=|{}.!'
    return ''.join(f'\\{char}' if char in special_chars else char for char in str(text))

# ============= DATABASE FUNCTIONS =============

async def init_db():
    """Initialize database connection and create tables"""
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=2,
            max_size=10,
            command_timeout=60
        )
        
        # Create tables
        async with db_pool.acquire() as conn:
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
            
            logger.info("✅ Database tables created/verified")
        
        logger.info("✅ Database connected successfully!")
        return True
    except Exception as e:
        logger.error(f"❌ Database connection failed: {e}")
        return False

async def get_or_create_user(user_id: int, username: str = None, first_name: str = None, last_name: str = None):
    """Get user or create if doesn't exist"""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow('SELECT * FROM users WHERE user_id = $1', user_id)
        
        if row:
            await conn.execute('UPDATE users SET last_active = CURRENT_TIMESTAMP WHERE user_id = $1', user_id)
            return dict(row)
        else:
            await conn.execute('''
                INSERT INTO users (user_id, username, first_name, last_name)
                VALUES ($1, $2, $3, $4)
            ''', user_id, username, first_name, last_name)
            
            row = await conn.fetchrow('SELECT * FROM users WHERE user_id = $1', user_id)
            return dict(row)

async def add_project(user_id: int, name: str, url: str) -> bool:
    """Add a new project"""
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO projects (user_id, name, url) VALUES ($1, $2, $3)',
                user_id, name, url
            )
            return True
    except Exception as e:
        logger.error(f"Error adding project: {e}")
        return False

async def get_projects(user_id: int):
    """Get all projects for a user"""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT * FROM projects WHERE user_id = $1 ORDER BY created_at DESC',
            user_id
        )
        return [dict(row) for row in rows]

async def update_project(project_id: int, user_id: int, name: str = None, url: str = None) -> bool:
    """Update a project"""
    try:
        async with db_pool.acquire() as conn:
            if name:
                await conn.execute('''
                    UPDATE projects SET name = $1, updated_at = CURRENT_TIMESTAMP
                    WHERE id = $2 AND user_id = $3
                ''', name, project_id, user_id)
            elif url:
                await conn.execute('''
                    UPDATE projects SET url = $1, updated_at = CURRENT_TIMESTAMP
                    WHERE id = $2 AND user_id = $3
                ''', url, project_id, user_id)
            return True
    except Exception as e:
        logger.error(f"Error updating project: {e}")
        return False

async def delete_project(project_id: int, user_id: int) -> bool:
    """Delete a project"""
    try:
        async with db_pool.acquire() as conn:
            await conn.execute('DELETE FROM projects WHERE id = $1 AND user_id = $2', project_id, user_id)
            return True
    except Exception as e:
        logger.error(f"Error deleting project: {e}")
        return False

async def add_pomodoro_session(user_id: int, start_time, end_time, duration, completed=False):
    """Log Pomodoro session"""
    try:
        async with db_pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO pomodoro_sessions (user_id, start_time, end_time, duration, completed)
                VALUES ($1, $2, $3, $4, $5)
            ''', user_id, start_time, end_time, duration, completed)
    except Exception as e:
        logger.error(f"Error adding Pomodoro session: {e}")

# ============= GITHUB FUNCTIONS =============

async def get_git_data(username):
    """Fetch GitHub user data"""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            # Get latest pushed repo
            r = await client.get(
                f"https://api.github.com/users/{username}/repos",
                params={"sort": "pushed", "per_page": 1}
            )
            
            if r.status_code == 404:
                return {"error": f"User '{username}' not found"}
            
            if r.status_code == 403:
                return {"error": "GitHub API rate limit exceeded. Please try again later."}
            
            r.raise_for_status()
            repos = r.json()
            
            if not repos:
                return {"error": f"No public repositories found for '{username}'"}
            
            repo_name = repos[0]['name']
            
            # Get latest commit
            c = await client.get(
                f"https://api.github.com/repos/{username}/{repo_name}/commits",
                params={"per_page": 1}
            )
            
            commits = c.json() if c.status_code == 200 else []
            
            return {
                "repo": repo_name,
                "msg": commits[0]['commit']['message'] if commits else "N/A",
                "date": repos[0]['pushed_at'],
                "link": repos[0]['html_url']
            }
            
        except Exception as e:
            logger.error(f"GitHub API error: {e}")
            return {"error": f"Error: {str(e)}"}

# ============= MAIN MENU & START =============

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command with menu"""
    user = update.effective_user
    
    # Register user
    await get_or_create_user(user.id, user.username, user.first_name, user.last_name)
    
    # Create menu keyboard
    keyboard = [
        ["➕ Add Project", "📂 List Projects"],
        ["🖥️ Check GitHub", "⏳ Pomodoro"],
        ["✏️ Edit Project", "🗑️ Delete Project"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        f"🚀 **Welcome {user.first_name}!**\n\n"
        f"I'm your Project Manager Bot. Choose an option below.\n\n"
        f"📌 **Commands:**\n"
        f"/start - Show menu\n"
        f"/add - Add project\n"
        f"/list - View projects\n"
        f"/edit - Edit project\n"
        f"/delete - Delete project\n"
        f"/git - Check GitHub\n"
        f"/study - Start timer\n"
        f"/stop - Stop timer\n"
        f"/cancel - Cancel operation",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

# ============= ADD PROJECT FLOW =============

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start add project flow"""
    await update.message.reply_text("📁 **Enter the Project Name:**", parse_mode='Markdown')
    return NAME

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get project name"""
    context.user_data['name'] = update.message.text.strip()
    await update.message.reply_text("🔗 **Enter the Project URL** (must include http:// or https://):", parse_mode='Markdown')
    return URL

async def get_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get and validate URL"""
    url = update.message.text.strip()
    
    if not url.startswith(('http://', 'https://')):
        await update.message.reply_text("❌ Invalid URL! Please include http:// or https://\n\nTry again:")
        return URL
    
    context.user_data['url'] = url
    
    # Confirmation buttons
    keyboard = [[
        InlineKeyboardButton("✅ Confirm", callback_data="confirm_yes"),
        InlineKeyboardButton("❌ Cancel", callback_data="confirm_no")
    ]]
    
    await update.message.reply_text(
        f"📝 **Confirm Project Details**\n\n"
        f"**Name:** {escape_markdown(context.user_data['name'])}\n"
        f"**URL:** {url}\n\n"
        f"Save this project?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return CONFIRM

async def confirm_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save or cancel project"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "confirm_yes":
        user_id = update.effective_user.id
        success = await add_project(user_id, context.user_data['name'], context.user_data['url'])
        
        if success:
            await query.edit_message_text(
                f"✅ **Project saved successfully!**\n\n"
                f"📁 {escape_markdown(context.user_data['name'])}\n"
                f"🔗 {context.user_data['url']}",
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text("❌ Failed to save project. Please try again.")
    else:
        await query.edit_message_text("❌ Action cancelled.")
    
    return ConversationHandler.END

# ============= LIST PROJECTS =============

async def list_projects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all projects"""
    user_id = update.effective_user.id
    projects = await get_projects(user_id)
    
    if not projects:
        await update.message.reply_text("📭 Your project list is currently empty.\n\nUse /add to add a project!")
        return
    
    text = "📂 **Your Projects:**\n\n"
    for i, p in enumerate(projects, 1):
        safe_name = escape_markdown(p['name'])
        created = p['created_at'].strftime("%Y-%m-%d %H:%M") if p['created_at'] else "Unknown"
        text += f"{i}. **[{safe_name}]({p['url']})**\n   📅 Added: {created}\n\n"
        
        if len(text) > 3500:
            await update.message.reply_text(text, parse_mode='Markdown', disable_web_page_preview=True)
            text = ""
    
    if text:
        await update.message.reply_text(text, parse_mode='Markdown', disable_web_page_preview=True)

# ============= EDIT PROJECT FLOW =============

async def edit_project_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start edit project flow"""
    user_id = update.effective_user.id
    projects = await get_projects(user_id)
    
    if not projects:
        await update.message.reply_text("No projects to edit.")
        return ConversationHandler.END
    
    keyboard = []
    for p in projects:
        keyboard.append([InlineKeyboardButton(escape_markdown(p['name']), callback_data=f"edit_{p['id']}")])
    
    await update.message.reply_text(
        "✏️ **Select project to edit:**",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return EDIT_PROJECT

async def edit_project_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle project selection"""
    query = update.callback_query
    await query.answer()
    
    try:
        project_id = int(query.data.split('_')[1])
    except (IndexError, ValueError):
        await query.edit_message_text("❌ Invalid selection.")
        return ConversationHandler.END
    
    context.user_data['edit_id'] = project_id
    
    user_id = update.effective_user.id
    projects = await get_projects(user_id)
    project = next((p for p in projects if p['id'] == project_id), None)
    
    if not project:
        await query.edit_message_text("❌ Project not found.")
        return ConversationHandler.END
    
    keyboard = [
        [InlineKeyboardButton("📝 Edit Name", callback_data="edit_name")],
        [InlineKeyboardButton("🔗 Edit URL", callback_data="edit_url")],
        [InlineKeyboardButton("❌ Cancel", callback_data="edit_cancel")]
    ]
    
    await query.edit_message_text(
        f"✏️ **Editing Project:**\n\n"
        f"**Name:** {escape_markdown(project['name'])}\n"
        f"**URL:** {project['url']}\n\n"
        f"What would you like to edit?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return EDIT_PROJECT

async def edit_project_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle edit field selection"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "edit_name":
        await query.edit_message_text("📝 **Enter the new project name:**", parse_mode='Markdown')
        context.user_data['edit_field'] = 'name'
        return EDIT_FIELD
    elif query.data == "edit_url":
        await query.edit_message_text("🔗 **Enter the new URL** (with http:// or https://):", parse_mode='Markdown')
        context.user_data['edit_field'] = 'url'
        return EDIT_FIELD
    else:
        await query.edit_message_text("❌ Edit cancelled.")
        return ConversationHandler.END

async def edit_project_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save edited project"""
    new_value = update.message.text.strip()
    field = context.user_data.get('edit_field')
    project_id = context.user_data.get('edit_id')
    user_id = update.effective_user.id
    
    if not field or not project_id:
        await update.message.reply_text("❌ Session expired. Please start over.")
        return ConversationHandler.END
    
    if field == 'url' and not new_value.startswith(('http://', 'https://')):
        await update.message.reply_text("❌ Invalid URL. Include http:// or https://\n\nTry again:")
        return EDIT_FIELD
    
    if field == 'name':
        success = await update_project(project_id, user_id, name=new_value)
    else:
        success = await update_project(project_id, user_id, url=new_value)
    
    if success:
        await update.message.reply_text(f"✅ Project {field} updated successfully!")
    else:
        await update.message.reply_text("❌ Failed to update project.")
    
    return ConversationHandler.END

# ============= DELETE PROJECT FLOW =============

async def delete_project_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start delete project flow"""
    user_id = update.effective_user.id
    projects = await get_projects(user_id)
    
    if not projects:
        await update.message.reply_text("No projects to delete.")
        return ConversationHandler.END
    
    keyboard = []
    for p in projects:
        keyboard.append([InlineKeyboardButton(escape_markdown(p['name']), callback_data=f"del_{p['id']}")])
    
    await update.message.reply_text(
        "🗑️ **Select project to delete:**",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return DELETE_PROJECT_CONFIRM

async def delete_project_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirm deletion"""
    query = update.callback_query
    await query.answer()
    
    try:
        project_id = int(query.data.split('_')[1])
    except (IndexError, ValueError):
        await query.edit_message_text("❌ Invalid selection.")
        return ConversationHandler.END
    
    context.user_data['delete_id'] = project_id
    
    keyboard = [
        [InlineKeyboardButton("✅ Yes, Delete", callback_data="del_yes")],
        [InlineKeyboardButton("❌ No, Cancel", callback_data="del_no")]
    ]
    
    await query.edit_message_text(
        "⚠️ **Are you sure?**\n\nThis action cannot be undone.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return DELETE_PROJECT_CONFIRM

async def delete_project_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Execute deletion"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "del_yes":
        project_id = context.user_data.get('delete_id')
        user_id = update.effective_user.id
        
        if not project_id:
            await query.edit_message_text("❌ Session expired. Please start over.")
            return ConversationHandler.END
        
        success = await delete_project(project_id, user_id)
        
        if success:
            await query.edit_message_text("✅ Project deleted successfully!")
        else:
            await query.edit_message_text("❌ Failed to delete project.")
    else:
        await query.edit_message_text("❌ Deletion cancelled.")
    
    return ConversationHandler.END

# ============= GITHUB FLOW =============

async def git_start_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start GitHub lookup"""
    sent_msg = await update.message.reply_text("👤 **GitHub Lookup**\n\nPlease type the username:", parse_mode='Markdown')
    context.user_data['git_msg_id'] = sent_msg.message_id
    return GET_GIT_USER

async def git_handle_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle GitHub username and fetch data"""
    username = update.message.text.strip()
    await update.message.delete()  # Delete user's message
    
    data = await get_git_data(username)
    
    if isinstance(data, dict) and "error" in data:
        text = f"❌ {data['error']}"
    elif data:
        dt = datetime.fromisoformat(data['date'].replace('Z', '+00:00')).strftime("%b %d, %Y at %H:%M")
        text = (f"🖥️ **Latest Push: {username}**\n\n"
                f"📂 **Repo:** {escape_markdown(data['repo'])}\n"
                f"📝 **Message:** {escape_markdown(data['msg'][:100])}\n"
                f"🕒 **Pushed:** {dt}\n"
                f"🔗 [View Repository]({data['link']})")
    else:
        text = f"❌ No public repos found for '{username}'"
    
    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=context.user_data['git_msg_id'],
        text=text,
        parse_mode='Markdown',
        disable_web_page_preview=True
    )
    
    return ConversationHandler.END

# ============= POMODORO TIMER =============

async def study_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start Pomodoro timer"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if chat_id in active_timers:
        await update.message.reply_text(
            "⏰ A timer is already running!\nUse /stop to cancel it before starting a new one."
        )
        return
    
    # Start timer in background
    timer_task = asyncio.create_task(run_pomodoro(update, context))
    active_timers[chat_id] = timer_task
    
    await update.message.reply_text(
        "⏳ **Pomodoro Started!**\n\n"
        f"🎯 Work: 50 minutes\n"
        "💡 Use /stop to cancel the timer\n"
        "⏰ I'll notify you when it's break time!",
        parse_mode='Markdown'
    )

async def run_pomodoro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run the Pomodoro timer"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    start_time = datetime.now()
    
    try:
        # Work period (50 minutes = 3000 seconds)
        for remaining in range(3000, 0, -60):
            if chat_id not in active_timers:
                await add_pomodoro_session(user_id, start_time, datetime.now(), 3000, False)
                return
            
            if remaining % 300 == 0:  # Every 5 minutes
                minutes = remaining // 60
                await update.message.reply_text(f"⏰ {minutes} minutes remaining... Keep going!")
            
            await asyncio.sleep(60)
        
        if chat_id in active_timers:
            await update.message.reply_text(
                "🎉 **Great work!** Time for a break!\n\n"
                "☕ Take 10 minutes to rest.\n"
                "I'll remind you to start your next session.",
                parse_mode='Markdown'
            )
            await add_pomodoro_session(user_id, start_time, datetime.now(), 3000, True)
            
            # Break period (10 minutes = 600 seconds)
            for remaining in range(600, 0, -60):
                if chat_id not in active_timers:
                    return
                await asyncio.sleep(60)
            
            if chat_id in active_timers:
                await update.message.reply_text(
                    "🎯 **Break's over!** Ready for another productive session?\n"
                    "Use /study to start a new Pomodoro!",
                    parse_mode='Markdown'
                )
                
    except asyncio.CancelledError:
        pass
    finally:
        active_timers.pop(chat_id, None)

async def stop_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop active timer"""
    chat_id = update.effective_chat.id
    
    if chat_id in active_timers:
        active_timers[chat_id].cancel()
        active_timers.pop(chat_id, None)
        await update.message.reply_text("⏹️ **Timer stopped.** Ready for your next session!")
    else:
        await update.message.reply_text("No active timer running.")

# ============= CANCEL & ERROR HANDLING =============

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel current operation"""
    context.user_data.clear()
    await update.message.reply_text("❌ **Operation cancelled.** Back to main menu.", parse_mode='Markdown')
    return ConversationHandler.END

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error(f"Update {update} caused error {context.error}")
    
    try:
        if update and update.effective_chat:
            await update.effective_chat.send_message(
                "❌ An unexpected error occurred. Please try again later."
            )
    except:
        pass

# ============= MAIN =============

async def main():
    """Start the bot"""
    # Initialize database
    if not await init_db():
        logger.error("Failed to connect to database. Exiting...")
        return
    
    # Create application
    app = ApplicationBuilder().token(TOKEN).build()
    
    # Error handler
    app.add_error_handler(error_handler)
    
    # Filter to block commands during conversation
    block_filter = filters.COMMAND & ~filters.Regex('^/cancel$')
    
    # Conversation: Add Project
    add_conv = ConversationHandler(
        entry_points=[
            CommandHandler("add", add_start),
            MessageHandler(filters.Regex("^➕ Add Project$"), add_start)
        ],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name), 
                   MessageHandler(block_filter, cancel)],
            URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_url), 
                  MessageHandler(block_filter, cancel)],
            CONFIRM: [CallbackQueryHandler(confirm_add, pattern="^confirm_"), 
                      MessageHandler(block_filter, cancel)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )
    
    # Conversation: Edit Project
    edit_conv = ConversationHandler(
        entry_points=[
            CommandHandler("edit", edit_project_start),
            MessageHandler(filters.Regex("^✏️ Edit Project$"), edit_project_start)
        ],
        states={
            EDIT_PROJECT: [
                CallbackQueryHandler(edit_project_select, pattern="^edit_\\d+$"),
                CallbackQueryHandler(edit_project_field, pattern="^edit_"),
                CallbackQueryHandler(edit_project_field, pattern="^edit_cancel$")
            ],
            EDIT_FIELD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_project_save),
                MessageHandler(block_filter, cancel)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )
    
    # Conversation: Delete Project
    delete_conv = ConversationHandler(
        entry_points=[
            CommandHandler("delete", delete_project_start),
            MessageHandler(filters.Regex("^🗑️ Delete Project$"), delete_project_start)
        ],
        states={
            DELETE_PROJECT_CONFIRM: [
                CallbackQueryHandler(delete_project_confirm, pattern="^del_\\d+$"),
                CallbackQueryHandler(delete_project_execute, pattern="^del_"),
                MessageHandler(block_filter, cancel)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )
    
    # Conversation: GitHub Check
    git_conv = ConversationHandler(
        entry_points=[
            CommandHandler("git", git_start_flow),
            MessageHandler(filters.Regex("^🖥️ Check GitHub$"), git_start_flow)
        ],
        states={
            GET_GIT_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, git_handle_user), 
                          MessageHandler(block_filter, cancel)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )
    
    # Add all conversation handlers
    app.add_handler(add_conv)
    app.add_handler(edit_conv)
    app.add_handler(delete_conv)
    app.add_handler(git_conv)
    
    # Command handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("list", list_projects))
    app.add_handler(CommandHandler("study", study_cmd))
    app.add_handler(CommandHandler("stop", stop_timer))
    
    # Menu button handlers
    app.add_handler(MessageHandler(filters.Regex("^📂 List Projects$"), list_projects))
    app.add_handler(MessageHandler(filters.Regex("^⏳ Pomodoro$"), study_cmd))
    
    # Set bot commands
    await app.bot.set_my_commands([
        BotCommand("start", "Show menu"),
        BotCommand("add", "Add a project"),
        BotCommand("list", "List projects"),
        BotCommand("edit", "Edit a project"),
        BotCommand("delete", "Delete a project"),
        BotCommand("git", "Check GitHub profile"),
        BotCommand("study", "Start Pomodoro timer"),
        BotCommand("stop", "Stop timer"),
        BotCommand("cancel", "Cancel operation")
    ])
    
    logger.info("🚀 Bot is running with full features!")
    
    # Start bot
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    
    # Keep running
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())