# Telegram Project Manager Bot

A full-featured Telegram bot for managing projects with GitHub integration and Pomodoro timer.

## Features

- ✅ Add, edit, delete projects
- ✅ List all saved projects
- ✅ GitHub profile checker
- ✅ Pomodoro timer (50 min work + 10 min break)
- ✅ Persistent PostgreSQL database
- ✅ User data stored in cloud

## Commands

- `/start` - Show main menu
- `/add` - Add a new project
- `/list` - View all projects
- `/edit` - Edit a project
- `/delete` - Delete a project
- `/git` - Check GitHub profile
- `/study` - Start Pomodoro timer
- `/stop` - Stop active timer
- `/cancel` - Cancel operation

## Setup

1. Install Python 3.9+
2. Install dependencies: `pip install -r requirements.txt`
3. Create `.env` file with your tokens
4. Run: `python bot.py`