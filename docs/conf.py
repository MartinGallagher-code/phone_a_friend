# SPDX-FileCopyrightText: 2026 Martin Gallagher
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Sphinx configuration for the phone_a_friend documentation."""

from phone_a_friend import __version__

project = "phone_a_friend"
copyright = "2026 Martin Gallagher"
author = "Martin Gallagher"
version = __version__
release = __version__

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
]

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "cryptography": ("https://cryptography.io/en/latest", None),
}

autodoc_member_order = "bysource"
autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
}

# The GitHub Pages landing page lives in this directory too; it is not a
# Sphinx source file.
exclude_patterns = ["_build", "index.html", "requirements.txt"]

html_theme = "sphinx_rtd_theme"
html_theme_options = {
    "collapse_navigation": False,
}
