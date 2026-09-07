"""Services: the core business logic.

    storage_service   user input -> filesystem paths, and bytes in and out of ./storage
    extract_service   document -> markdown, via MarkItDown
    library_service   folder and file operations, composed from the two above
    ollama_service    inference: the chat and generate streams

Depends on `models`, never the other way round: models run SQL and nothing else.
Routes depend on services, and hold no logic of their own.
"""
