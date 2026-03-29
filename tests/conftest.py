"""
Adds src/ to sys.path so tests can import schema_extractor.* and metadata_store
without installing the project as a package.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
