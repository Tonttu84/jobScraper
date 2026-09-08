"""Small FastAPI app over ``jobs.db``: a JSON API plus a single-page UI.

``jobscraper serve`` builds it with :func:`jobscraper.web.app.create_app` and runs it with
uvicorn. There is no authentication — keep it on localhost or behind a proxy.
"""
