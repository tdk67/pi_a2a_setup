#!/usr/bin/env python3
"""
mem0 Memory Service - Semantic memory storage for pi agent and telegram bot

Uses mem0.ai library with:
- Local embeddings (sentence-transformers)
- Local Qdrant vector store
- No LLM extraction (we pre-process in TypeScript)

Provides HTTP API for:
- POST /memory/add - Add a memory
- POST /memory/search - Search memories
- GET /memory/list - List all memories
- DELETE /memory/delete/<id> - Delete a memory
- GET /health - Health check

Diary API (new):
- POST /api/memories - Add a memory
- GET /api/memories - List all memories
- GET /api/search - Search memories
- DELETE /api/memories/<id> - Delete a memory
- GET /api/recap - Get recent memories (optimized for /recap command)
- GET /api/projects - List projects with counts
"""

import os
import sys
import json
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from mem0 import Memory

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Initialize mem0 with local components
os.environ['HF_HOME'] = '/root/.cache/huggingface'

config = {
    "embedder": {
        "provider": "huggingface",
        "config": {"model": "sentence-transformers/all-MiniLM-L6-v2"}
    },
    "vector_store": {
        "provider": "qdrant",
        "config": {
            "collection_name": "pi_memory",
            "embedding_model_dims": 384,
            "path": "/root/.pi/agent/qdrant_data",
            "on_disk": True
        }
    },
    # Dummy LLM config - we won't use it (infer=False)
    "llm": {
        "provider": "openai",
        "config": {"model": "dummy", "api_key": "dummy", "openai_base_url": "http://localhost:1"}
    }
}

logger.info("Initializing mem0 with local embeddings and Qdrant...")
memory = Memory.from_config(config)
logger.info("✓ mem0 initialized")

PORT = 7011


class MemoryHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        logger.info(format % args)

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def read_body(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        return json.loads(body) if body else {}

    def do_GET(self):
        parsed = urlparse(self.path)
        
        if parsed.path == '/health':
            self.send_json({'status': 'ok', 'service': 'mem0'})
        
        elif parsed.path == '/memory/list':
            params = parse_qs(parsed.query)
            user_id = params.get('user_id', ['global'])[0]
            limit = int(params.get('limit', ['100'])[0])
            
            try:
                # Use filters parameter for get_all
                results = memory.get_all(filters={"user_id": user_id}, top_k=999999)
                memories = results.get('results', [])
                # Apply limit
                memories = memories[:limit]
                self.send_json({'count': len(memories), 'memories': memories})
            except Exception as e:
                logger.error(f"Error listing memories: {e}")
                self.send_json({'error': str(e)}, 500)
        
        # ── Diary API endpoints ──────────────────────────────────────
        elif parsed.path == '/api/memories':
            # GET /api/memories?limit=50&offset=0&user_id=pi-agent
            params = parse_qs(parsed.query)
            limit = int(params.get('limit', ['50'])[0])
            offset = int(params.get('offset', ['0'])[0])
            user_id = params.get('user_id', [None])[0]
            
            try:
                if user_id:
                    results = memory.get_all(filters={"user_id": user_id}, top_k=999999)
                else:
                    # Query all known user_ids
                    all_memories = []
                    for uid in ['pi-agent', 'telegram-user', 'global']:
                        try:
                            results = memory.get_all(filters={"user_id": uid}, top_k=999999)
                            all_memories.extend(results.get('results', []))
                        except:
                            pass
                    results = {'results': all_memories}
                
                all_memories = results.get('results', [])
                # Sort by timestamp (newest first)
                all_memories.sort(key=lambda m: m.get('created_at', ''), reverse=True)
                total_count = len(all_memories)
                # Apply pagination
                paginated = all_memories[offset:offset+limit]
                self.send_json({
                    'memories': paginated,
                    'total': total_count,
                    'limit': limit,
                    'offset': offset
                })
            except Exception as e:
                logger.error(f"Error listing memories (API): {e}")
                self.send_json({'error': str(e)}, 500)
        
        elif parsed.path == '/api/search':
            # GET /api/search?q=query&limit=10&user_id=pi-agent
            params = parse_qs(parsed.query)
            query = params.get('q', [''])[0]
            limit = int(params.get('limit', ['10'])[0])
            user_id = params.get('user_id', [None])[0]
            
            if not query:
                self.send_json({'error': 'query parameter q is required'}, 400)
                return
            
            try:
                if user_id:
                    results = memory.search(query, filters={"user_id": user_id}, limit=limit)
                else:
                    # Search across all known user_ids
                    all_results = []
                    for uid in ['pi-agent', 'telegram-user', 'global']:
                        try:
                            results = memory.search(query, filters={"user_id": uid}, limit=limit)
                            all_results.extend(results.get('results', []))
                        except:
                            pass
                    # Sort by score and limit
                    all_results.sort(key=lambda m: m.get('score', 0), reverse=True)
                    results = {'results': all_results[:limit]}
                
                memories = results.get('results', [])
                self.send_json({'count': len(memories), 'results': memories})
            except Exception as e:
                logger.error(f"Error searching memories (API): {e}")
                self.send_json({'error': str(e)}, 500)
        
        elif parsed.path == '/api/recap':
            # GET /api/recap?topic=topic&limit=5&user_id=pi-agent
            params = parse_qs(parsed.query)
            topic = params.get('topic', [''])[0]
            limit = int(params.get('limit', ['5'])[0])
            user_id = params.get('user_id', [None])[0]
            limit = min(limit, 20)  # Cap at 20
            
            try:
                if topic:
                    # Semantic search for topic
                    if user_id:
                        results = memory.search(topic, filters={"user_id": user_id}, limit=limit)
                        memories = results.get('results', [])
                    else:
                        # Search across all known user_ids
                        all_results = []
                        for uid in ['pi-agent', 'telegram-user', 'global']:
                            try:
                                results = memory.search(topic, filters={"user_id": uid}, limit=limit)
                                all_results.extend(results.get('results', []))
                            except:
                                pass
                        all_results.sort(key=lambda m: m.get('score', 0), reverse=True)
                        memories = all_results[:limit]
                else:
                    # Get most recent memories
                    if user_id:
                        results = memory.get_all(filters={"user_id": user_id}, top_k=999999)
                        all_memories = results.get('results', [])
                    else:
                        # Get from all known user_ids
                        all_memories = []
                        for uid in ['pi-agent', 'telegram-user', 'global']:
                            try:
                                results = memory.get_all(filters={"user_id": uid}, top_k=999999)
                                all_memories.extend(results.get('results', []))
                            except:
                                pass
                    # Sort by timestamp (newest first)
                    all_memories.sort(key=lambda m: m.get('created_at', ''), reverse=True)
                    memories = all_memories[:limit]
                
                self.send_json({'results': memories, 'topic': topic, 'limit': limit})
            except Exception as e:
                logger.error(f"Error getting recap: {e}")
                self.send_json({'error': str(e)}, 500)
        
        elif parsed.path == '/api/projects':
            # GET /api/projects - list unique projects with counts
            try:
                all_memories = []
                for uid in ['pi-agent', 'telegram-user', 'global']:
                    try:
                        results = memory.get_all(filters={"user_id": uid}, top_k=999999)
                        all_memories.extend(results.get('results', []))
                    except:
                        pass
                
                # Count by project
                project_counts = {}
                for mem in all_memories:
                    project = mem.get('metadata', {}).get('project', 'unknown')
                    project_counts[project] = project_counts.get(project, 0) + 1
                
                # Convert to list of dicts
                projects = [{'name': k, 'count': v} for k, v in project_counts.items()]
                # Sort by count descending
                projects.sort(key=lambda p: p['count'], reverse=True)
                
                self.send_json({'projects': projects})
            except Exception as e:
                logger.error(f"Error listing projects: {e}")
                self.send_json({'error': str(e)}, 500)
        
        else:
            self.send_json({'error': 'not found'}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        
        if parsed.path == '/memory/add':
            try:
                data = self.read_body()
                text = data.get('text', '')
                user_id = data.get('user_id', 'global')
                metadata = data.get('metadata', {})
                
                if not text:
                    self.send_json({'error': 'text is required'}, 400)
                    return
                
                # Add memory without LLM extraction
                result = memory.add(text, user_id=user_id, metadata=metadata, infer=False)
                self.send_json({'status': 'ok', 'result': result})
                
            except Exception as e:
                logger.error(f"Error adding memory: {e}")
                self.send_json({'error': str(e)}, 500)
        
        elif parsed.path == '/memory/search':
            try:
                data = self.read_body()
                query = data.get('query', '')
                user_id = data.get('user_id', 'global')
                limit = data.get('limit', 10)
                
                if not query:
                    self.send_json({'error': 'query is required'}, 400)
                    return
                
                # Search with semantic similarity
                results = memory.search(query, filters={"user_id": user_id}, limit=limit)
                memories = results.get('results', [])
                
                self.send_json({'count': len(memories), 'results': memories})
                
            except Exception as e:
                logger.error(f"Error searching memories: {e}")
                self.send_json({'error': str(e)}, 500)
        
        # ── Diary API endpoints ──────────────────────────────────────
        elif parsed.path == '/api/memories':
            # POST /api/memories - add a memory
            try:
                data = self.read_body()
                text = data.get('text', '')
                source = data.get('source', 'api')  # telegram | pi | api
                project = data.get('project', None)
                metadata = data.get('metadata', {})
                
                if not text:
                    self.send_json({'error': 'text is required'}, 400)
                    return
                
                # Merge source and project into metadata
                metadata['source'] = source
                if project:
                    metadata['project'] = project
                
                result = memory.add(text, user_id='pi-agent', metadata=metadata, infer=False)
                
                # Extract ID from result
                mem_id = None
                if result.get('results'):
                    mem_id = result['results'][0].get('id')
                
                self.send_json({
                    'id': mem_id,
                    'timestamp': result.get('timestamp') or data.get('timestamp', '')
                }, 201)
                
            except Exception as e:
                logger.error(f"Error adding memory (API): {e}")
                self.send_json({'error': str(e)}, 500)
        
        else:
            self.send_json({'error': 'not found'}, 404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        
        if parsed.path.startswith('/memory/delete/'):
            memory_id = parsed.path.split('/memory/delete/')[1]
            
            try:
                memory.delete(memory_id)
                self.send_json({'status': 'ok', 'deleted': memory_id})
            except Exception as e:
                logger.error(f"Error deleting memory: {e}")
                self.send_json({'error': str(e)}, 500)
        
        # ── Diary API endpoint ──────────────────────────────────────
        elif parsed.path.startswith('/api/memories/'):
            # DELETE /api/memories/<id>
            memory_id = parsed.path.split('/api/memories/')[1]
            
            try:
                memory.delete(memory_id)
                self.send_json({'status': 'ok', 'deleted': memory_id}, 204)
            except Exception as e:
                logger.error(f"Error deleting memory (API): {e}")
                self.send_json({'error': str(e)}, 500)
        
        else:
            self.send_json({'error': 'not found'}, 404)


if __name__ == '__main__':
    logger.info(f"Starting mem0 service on port {PORT}")
    server = HTTPServer(('127.0.0.1', PORT), MemoryHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        server.shutdown()
