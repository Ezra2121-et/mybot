import os
import asyncio
import logging
import warnings
from datetime import datetime, timezone
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, 
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters
)
from telegram.warnings import PTBUserWarning
from database import db
from github_handler import get_git_data  # Create separate file for GitHub handler

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

warnings.filterwarnings("ignore", category=PTBUserWarning)

load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not TOKEN:
    raise ValueError("TELEGRAM_TOKEN not found")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL not found")

# Constants for State Machine
NAME, URL, CONFIRM, GET_GIT_USER, EDIT_PROJECT, EDIT_FIELD, DELETE_PROJECT_CONFIRM = range(7)

active_timers = {}
start_time = datetime.now()

def escape_markdown(text):
    """Escape special characters for Markdown"""
    special_chars = r'_*[]()~`>#+-=|{}.!'
    return ''.join(f'\\{char}' if char in special_chars else char for char in str(text))

# --- Main Menu & Start ---

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sets up the persistent keyboard menu."""
    user = update.effective_user
    
    # Register user in database
    await db.get_or_create_user(
        user.id,
        user.username,
        user.first_name,
        user.last_name
    )
    
    keyboard = [
        ["➕ Add Project", "📂 List Projects"],
        ["🖥️ Check GitHub", "⏳ Pomodoro"],
        ["✏️ Edit Project", "🗑️ Delete Project"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        f"🚀 **Welcome {user.first_name}!**\n\n"
        "I'm your Project Manager Bot. Choose an option from the menu below.\n\n"
        "📌 **Commands:**\n"
        "/start - Restart the bot\n"
        "/add - Add a new project\n"
        "/list - View saved projects\n"
        "/git - Check GitHub profile\n"
        "/study - Start Pomodoro timer\n"
        "/stop - Stop active timer\n"
        "/cancel - Cancel current operation",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

# --- Project Add Flow ---

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start project addition flow"""
    await update.message.reply_text("📁 Enter the **Project Name**:")
    return NAME

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get project name"""
    context.user_data['n'] = update.message.text.strip()
    await update.message.reply_text("🔗 Enter the **Project URL** (must include http:// or https://):")
    return URL

async def get_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get and validate project URL"""
    url = update.message.text.strip()
    
    if not url.startswith(('http://', 'https://')):
        await update.message.reply_text("❌ Invalid URL. Please include http:// or https://\n\nTry again:")
        return URL
    
    context.user_data['u'] = url
    
    kb = [[InlineKeyboardButton("✅ Confirm", callback_data="c_y"), 
           InlineKeyboardButton("❌ Cancel", callback_data="c_n")]]
    
    await update.message.reply_text(
        f"📝 **Confirm Project Details**\n\n"
        f"**Name:** {escape_markdown(context.user_data['n'])}\n"
        f"**URL:** {url}\n\n"
        f"Save this project?",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode='Markdown'
    )
    return CONFIRM

async def confirm_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirm and save project"""
    q = update.callback_query
    await q.answer()
    
    if q.data == "c_y":
        user_id = update.effective_user.id
        success = await db.add_project(user_id, context.user_data['n'], context.user_data['u'])
        
        if success:
            await q.edit_message_text(
                f"✅ **Project saved successfully!**\n\n"
                f"📁 {escape_markdown(context.user_data['n'])}\n"
                f"🔗 {context.user_data['u']}"
            )
        else:
            await q.edit_message_text("❌ Failed to save project. Please try again.")
    else:
        await q.edit_message_text("❌ Action cancelled.")
    
    return ConversationHandler.END

# --- Project Listing ---

async def list_projects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all saved projects"""
    user_id = update.effective_user.id
    projects = await db.get_projects(user_id)
    
    if not projects:
        return await update.message.reply_text("📭 Your project list is currently empty.")
    
    text = "📂 **Your Projects:**\n\n"
    for i, p in enumerate(projects, 1):
        safe_name = escape_markdown(p['name'])
        created_date = p['created_at'].strftime("%Y-%m-%d %H:%M") if p['created_at'] else "Unknown date"
        text += f"{i}. **[{safe_name}]({p['url']})**\n   📅 Added: {created_date}\n\n"
        
        if len(text) > 3500:
            await update.message.reply_text(text, parse_mode='Markdown', disable_web_page_preview=True)
            text = ""
    
    if text:
        await update.message.reply_text(text, parse_mode='Markdown', disable_web_page_preview=True)

# --- GitHub Flow ---

async def git_start_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for GitHub lookup."""
    sent_msg = await update.message.reply_text("👤 **GitHub Lookup**\n\nPlease type the username:")
    context.user_data['git_msg_id'] = sent_msg.message_id
    return GET_GIT_USER

async def git_handle_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetches data and edits the prompt message."""
    username = update.message.text.strip()
    await update.message.delete()
    
    try:
        data = await get_git_data(username)
        
        if isinstance(data, dict) and "error" in data:
            text = f"❌ {data['error']}"
        elif data:
            dt = datetime.fromisoformat(data['date'].replace('Z', '+00:00')).strftime("%b %d, %Y at %H:%M")
            text = (f"🖥️ **Latest Push: {username}**\n\n"
                    f"📂 **Repo:** {escape_markdown(data['repo'])}\n"
                    f"📝 **Message:** {escape_markdown(data['msg'])}\n"
                    f"🕒 **Pushed:** {dt}\n"
                    f"🔗 [View Repository]({data['link']})")
        else:
            text = f"❌ No public repos found for '{username}'."

        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=context.user_data['git_msg_id'],
            text=text,
            parse_mode='Markdown',
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"Git handler error: {e}")
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=context.user_data['git_msg_id'],
            text="❌ An unexpected error occurred. Please try again later."
        )
    
    return ConversationHandler.END

# --- Pomodoro Timer ---

async def study_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start Pomodoro timer"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if chat_id in active_timers:
        await update.message.reply_text(
            "⏰ A timer is already running!\nUse /stop to cancel it before starting a new one."
        )
        return
    
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
    """Run the actual Pomodoro timer"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    start_time = datetime.now()
    
    try:
        # Work period (50 minutes)
        for remaining in range(50 * 60, 0, -60):
            if chat_id not in active_timers:
                await db.add_pomodoro_session(user_id, start_time, datetime.now(), 50 * 60, False)
                return
            
            if remaining % 300 == 0:
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
            await db.add_pomodoro_session(user_id, start_time, datetime.now(), 50 * 60, True)
            
            # Break period (10 minutes)
            for remaining in range(10 * 60, 0, -60):
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
    """Stop the active timer"""
    chat_id = update.effective_chat.id
    
    if chat_id in active_timers:
        timer_task = active_timers[chat_id]
        timer_task.cancel()
        active_timers.pop(chat_id, None)
        await update.message.reply_text("⏹️ **Timer stopped.** Ready for your next session!")
    else:
        await update.message.reply_text("No active timer running.")

# --- Edit Project Flow ---

async def edit_project_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start project edit flow"""
    user_id = update.effective_user.id
    projects = await db.get_projects(user_id)
    
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
    """Handle project selection for editing"""
    q = update.callback_query
    await q.answer()
    
    try:
        project_id = int(q.data.split('_')[1])
    except (IndexError, ValueError):
        await q.edit_message_text("❌ Invalid selection.")
        return ConversationHandler.END
    
    context.user_data['edit_project_id'] = project_id
    
    user_id = update.effective_user.id
    projects = await db.get_projects(user_id)
    project = next((p for p in projects if p['id'] == project_id), None)
    
    if not project:
        await q.edit_message_text("❌ Project not found.")
        return ConversationHandler.END
    
    keyboard = [
        [InlineKeyboardButton("📝 Edit Name", callback_data="edit_field_name")],
        [InlineKeyboardButton("🔗 Edit URL", callback_data="edit_field_url")],
        [InlineKeyboardButton("❌ Cancel", callback_data="edit_cancel")]
    ]
    
    await q.edit_message_text(
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
    q = update.callback_query
    await q.answer()
    
    action = q.data
    
    if action == "edit_field_name":
        await q.edit_message_text("📝 Enter the new project name:")
        context.user_data['edit_field'] = 'name'
        return EDIT_FIELD
    elif action == "edit_field_url":
        await q.edit_message_text("🔗 Enter the new project URL (must include http:// or https://):")
        context.user_data['edit_field'] = 'url'
        return EDIT_FIELD
    else:
        await q.edit_message_text("❌ Edit cancelled.")
        return ConversationHandler.END

async def edit_project_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save edited project"""
    new_value = update.message.text.strip()
    field = context.user_data.get('edit_field')
    project_id = context.user_data.get('edit_project_id')
    user_id = update.effective_user.id
    
    if not field or not project_id:
        await update.message.reply_text("❌ Session expired. Please start over.")
        return ConversationHandler.END
    
    if field == 'url' and not new_value.startswith(('http://', 'https://')):
        await update.message.reply_text("❌ Invalid URL. Please include http:// or https://\n\nTry again:")
        return EDIT_FIELD
    
    if field == 'name':
        success = await db.update_project(project_id, user_id, name=new_value)
    else:
        success = await db.update_project(project_id, user_id, url=new_value)
    
    if success:
        await update.message.reply_text(f"✅ Project {field} updated successfully!")
    else:
        await update.message.reply_text("❌ Failed to update project.")
    
    return ConversationHandler.END

# --- Delete Project Flow ---

async def delete_project_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start project deletion flow"""
    user_id = update.effective_user.id
    projects = await db.get_projects(user_id)
    
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
    """Confirm project deletion"""
    q = update.callback_query
    await q.answer()
    
    try:
        project_id = int(q.data.split('_')[1])
    except (IndexError, ValueError):
        await q.edit_message_text("❌ Invalid selection.")
        return ConversationHandler.END
    
    context.user_data['delete_project_id'] = project_id
    
    user_id = update.effective_user.id
    projects = await db.get_projects(user_id)
    project = next((p for p in projects if p['id'] == project_id), None)
    
    if not project:
        await q.edit_message_text("❌ Project not found.")
        return ConversationHandler.END
    
    keyboard = [
        [InlineKeyboardButton("✅ Yes, Delete", callback_data="del_yes")],
        [InlineKeyboardButton("❌ No, Cancel", callback_data="del_no")]
    ]
    
    await q.edit_message_text(
        f"⚠️ **Delete Project**\n\n"
        f"Are you sure you want to delete '{escape_markdown(project['name'])}'?\n\n"
        f"This action cannot be undone.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return DELETE_PROJECT_CONFIRM

async def delete_project_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Execute project deletion"""
    q = update.callback_query
    await q.answer()
    
    if q.data == "del_yes":
        project_id = context.user_data.get('delete_project_id')
        user_id = update.effective_user.id
        
        if not project_id:
            await q.edit_message_text("❌ Session expired. Please start over.")
            return ConversationHandler.END
        
        success = await db.delete_project(project_id, user_id)
        
        if success:
            await q.edit_message_text("✅ Project deleted successfully!")
        else:
            await q.edit_message_text("❌ Failed to delete project.")
    else:
        await q.edit_message_text("❌ Deletion cancelled.")
    
    return ConversationHandler.END

# --- Cancel and Error Handling ---

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel current operation"""
    user_id = update.effective_user.id
    await db.clear_session(user_id)
    context.user_data.clear()
    await update.message.reply_text("❌ **Operation cancelled.** Back to main menu.")
    return ConversationHandler.END

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log errors and notify user"""
    logger.error(f"Update {update} caused error {context.error}")
    
    try:
        if update and update.effective_chat:
            await update.effective_chat.send_message(
                "❌ An unexpected error occurred. Please try again later."
            )
    except:
        pass

# --- Application Startup ---

async def post_init(application):
    """Initialize database and register commands"""
    await db.connect()
    await application.bot.set_my_commands([
        BotCommand("start", "Restart the bot and show menu"),
        BotCommand("add", "Add a new project"),
        BotCommand("list", "View saved projects"),
        BotCommand("edit", "Edit a project"),
        BotCommand("delete", "Delete a project"),
        BotCommand("git", "Check GitHub profile"),
        BotCommand("study", "Start Pomodoro timer"),
        BotCommand("stop", "Stop active timer"),
        BotCommand("cancel", "Cancel current operation")
    ])
    logger.info("Bot started successfully with database")

async def shutdown(application):
    """Cleanup on shutdown"""
    await db.close()
    logger.info("Bot shutdown complete")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).post_shutdown(shutdown).build()
    
    app.add_error_handler(error_handler)
    
    block_filter = filters.COMMAND & ~filters.Regex('^/cancel$')
    
    # Conversation handlers
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
            CONFIRM: [CallbackQueryHandler(confirm_add, pattern="^c_"), 
                      MessageHandler(block_filter, cancel)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )
    
    edit_conv = ConversationHandler(
        entry_points=[
            CommandHandler("edit", edit_project_start),
            MessageHandler(filters.Regex("^✏️ Edit Project$"), edit_project_start)
        ],
        states={
            EDIT_PROJECT: [
                CallbackQueryHandler(edit_project_select, pattern="^edit_\\d+$"),
                CallbackQueryHandler(edit_project_field, pattern="^edit_field_"),
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
    
    # Add handlers
    app.add_handler(add_conv)
    app.add_handler(edit_conv)
    app.add_handler(delete_conv)
    app.add_handler(git_conv)
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("list", list_projects))
    app.add_handler(CommandHandler("study", study_cmd))
    app.add_handler(CommandHandler("stop", stop_timer))
    app.add_handler(MessageHandler(filters.Regex("^📂 List Projects$"), list_projects))
    app.add_handler(MessageHandler(filters.Regex("^⏳ Pomodoro$"), study_cmd))
    
    logger.info("Bot is up and running with database...")
    app.run_polling()