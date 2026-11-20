"""FastAPI entry point for package imports and Render's root directory."""
if __package__:
    from .gmail_service import app
else:
    from gmail_service import app
