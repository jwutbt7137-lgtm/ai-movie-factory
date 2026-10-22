"""电影剧情理解（第二阶段：对白证据驱动的分段分析）。"""

from .story import StoryAnalyzer
from .segmentation import StoryChunk, build_story_chunks

__all__ = ["StoryAnalyzer", "StoryChunk", "build_story_chunks"]
