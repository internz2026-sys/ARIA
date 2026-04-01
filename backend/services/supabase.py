"""Centralized Supabase client — single import point for all services."""
from backend.config.loader import _get_supabase

def get_db():
    """Get the shared Supabase client instance."""
    return _get_supabase()
