class CartasRasterError(Exception):
    """Base error for the pipeline."""


class SourceLayoutError(CartasRasterError):
    """The DHN site no longer matches what the scraper understands.

    Callers must not publish a catalogue when this is raised: an empty or
    partial publish would look like every chart had been withdrawn.
    """
