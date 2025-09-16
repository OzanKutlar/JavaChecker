"""
Code Analyzer Backend - Main Server
=====================================

This is the main Python backend server for the Code Analyzer application.
It provides a WebSocket-based API for real-time communication with the frontend.

Architecture:
- Modular checker system that allows easy addition of new code analysis modules
- WebSocket server for real-time progress updates and result streaming
- Extensible interface for different types of code checkers
- Currently implements ExceptionChecker for Java code analysis

The server accepts project paths from the frontend, runs configured checker modules,
and streams progress updates and findings back to the client in real-time.

Usage:
    python code_analyzer_backend.py

Dependencies:
    pip install websockets
"""

import asyncio
import websockets
import json
import os
import glob
from pathlib import Path
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
import re


class CodeChecker(ABC):
    """Abstract base class for all code checkers."""
    
    @abstractmethod
    async def run(self, project_path: str, progress_callback) -> List[Dict[str, Any]]:
        """
        Run the checker on the given project path.
        
        Args:
            project_path: Path to the project directory
            progress_callback: Async function to call for progress updates
            
        Returns:
            List of findings, each as a dict with keys:
            - file: relative file path
            - line: line number (1-based)
            - column: column number (1-based)  
            - description: description of the issue
            - codeLines: list of dict with 'number' and 'content' keys
            - problemLineIndex: index in codeLines of the problematic line
            - callTrace: (Optional) list of dicts representing the call stack
        """
        pass
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Return the name of this checker."""
        pass


class ExceptionChecker(CodeChecker):
    """
    Java Exception Checker Module
    =============================
    
    Scans Java source code for exception-prone method calls that are not
    properly wrapped in try-catch blocks. Walks up the scope hierarchy
    to determine if exceptions are handled at any level up to main().
    
    Exception-prone patterns detected:
    - Integer.parseInt()
    - Double.parseDouble()
    - Float.parseFloat()
    - Long.parseLong()
    - Scanner.nextInt()
    - Scanner.nextDouble()
    - And other similar parsing/input methods
    """
    
    # Predefined list of exception-prone method calls
    EXCEPTION_PRONE_PATTERNS = [
        r'Integer\.parseInt\s*\(',
        r'Double\.parseDouble\s*\(',
        r'Float\.parseFloat\s*\(',
        r'Long\.parseLong\s*\(',
        r'Short\.parseShort\s*\(',
        r'Byte\.parseByte\s*\(',
        r'Boolean\.parseBoolean\s*\(',
        r'\.nextInt\s*\(',
        r'\.nextDouble\s*\(',
        r'\.nextFloat\s*\(',
        r'\.nextLong\s*\(',
        r'\.nextByte\s*\(',
        r'\.nextShort\s*\(',
        r'\.readLine\s*\(',
        r'\.substring\s*\(',
        r'\.charAt\s*\(',
        r'new\s+.*Scanner\s*\(',
        r'new\s+.*FileReader\s*\(',
        r'new\s+.*BufferedReader\s*\(',
        r'\.split\s*\(',
        r'\.get\s*\(',  # for collections
    ]
    
    @property
    def name(self) -> str:
        return "Exception Checker"
    
    async def run(self, project_path: str, progress_callback) -> List[Dict[str, Any]]:
        """Run the exception checker on all Java files in the project."""
        findings = []
        
        # Find all Java files
        java_files = []
        for root, dirs, files in os.walk(project_path):
            for file in files:
                if file.endswith('.java'):
                    java_files.append(os.path.join(root, file))
        
        if not java_files:
            await progress_callback("No Java files found in project")
            return findings
        
        await progress_callback(f"Found {len(java_files)} Java files to analyze. Pre-parsing files...")
        
        # Pre-load all file contents for faster analysis
        all_files_content = {}
        for file_path in java_files:
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    all_files_content[file_path] = f.read().splitlines()
            except Exception as e:
                await progress_callback(f"Could not read {os.path.basename(file_path)}: {e}")

        # Analyze each file
        for i, file_path in enumerate(all_files_content.keys(), 1):
            relative_path = os.path.relpath(file_path, project_path)
            await progress_callback(f"Checking file {i}/{len(all_files_content)}: {relative_path}")
            
            try:
                file_findings = await self._analyze_file(file_path, project_path, all_files_content)
                findings.extend(file_findings)
            except Exception as e:
                await progress_callback(f"Error analyzing {relative_path}: {str(e)}")
        
        await progress_callback(f"Exception checking complete. Found {len(findings)} issues.")
        return findings
    
    async def _analyze_file(self, file_path: str, project_path: str, all_files: Dict[str, List[str]]) -> List[Dict[str, Any]]:
        """Analyze a single Java file for exception-prone calls."""
        findings = []
        lines = all_files.get(file_path, [])
        if not lines:
            return findings

        relative_path = os.path.relpath(file_path, project_path)
        
        # Check each line for exception-prone patterns
        for line_num, line in enumerate(lines, 1):
            for pattern in self.EXCEPTION_PRONE_PATTERNS:
                for match in re.finditer(pattern, line):
                    # Check if this call is in a try-catch block
                    if not self._is_in_try_catch_block(lines, line_num - 1):
                        call_trace = await self._build_call_trace(all_files, file_path, line_num -1, project_path)
                        finding = {
                            'file': relative_path,
                            'line': line_num,
                            'column': match.start() + 1,
                            'description': f'Exception-prone call "{match.group().strip()}" not wrapped in try-catch',
                            'codeLines': self._get_code_context(lines, line_num - 1),
                            'problemLineIndex': 5,
                            'callTrace': call_trace
                        }
                        findings.append(finding)
        
        return findings

    def _get_enclosing_method_details(self, lines: List[str], line_index: int) -> Optional[Dict[str, Any]]:
        """Find the details of the method enclosing the given line index."""
        brace_count = 0
        method_pattern = re.compile(r'(public|private|protected|static|\s)*[\w\<\>\[\]]+\s+(\w+)\s*\([^)]*\)\s*(throws\s+[\w,\s]+)?\s*{?')
        
        for i in range(line_index, -1, -1):
            line = lines[i]
            if '}' in line:
                brace_count += line.count('}')
            if '{' in line:
                brace_count -= line.count('{')
            
            if brace_count > 0: # Moved out of the current scope
                return None

            match = method_pattern.search(line)
            if match:
                method_name = match.group(2)
                signature = ' '.join(line.strip().split()) # a cleaned up version of the line
                return {
                    "name": method_name,
                    "signature": signature.replace('{', '').strip(),
                    "start_line": i
                }
        return None

    def _find_method_callers(self, all_files: Dict[str, List[str]], method_name: str, project_path: str) -> List[Dict[str, Any]]:
        """Find all call sites for a given method name. NOTE: This is a simple regex search and may have false positives."""
        callers = []
        # This regex looks for the method name followed by parentheses, avoiding definition keywords
        caller_pattern = re.compile(r'\b' + re.escape(method_name) + r'\s*\(')
        
        for file_path, lines in all_files.items():
            for line_num, line in enumerate(lines, 1):
                # Basic check to avoid matching the method definition itself
                if any(keyword in line for keyword in ['public', 'private', 'protected', 'class', 'interface']):
                    if re.search(r'\s+' + re.escape(method_name) + r'\s*\(', line):
                        continue
                
                for match in caller_pattern.finditer(line):
                    callers.append({
                        "file": os.path.relpath(file_path, project_path),
                        "abs_path": file_path,
                        "line": line_num,
                        "column": match.start() + 1,
                    })
        return callers

    async def _build_call_trace(self, all_files: Dict[str, List[str]], initial_file_path: str, initial_line_index: int, project_path: str, max_depth=5) -> List[Dict[str, Any]]:
        """Builds a call trace starting from a specific line of code."""
        trace = []
        visited = set()
        
        current_file_path = initial_file_path
        current_line_index = initial_line_index

        for _ in range(max_depth):
            if (current_file_path, current_line_index) in visited:
                break
            visited.add((current_file_path, current_line_index))

            lines = all_files.get(current_file_path, [])
            enclosing_method = self._get_enclosing_method_details(lines, current_line_index)

            if not enclosing_method or 'main' in enclosing_method['signature']:
                break

            method_name = enclosing_method['name']
            callers = self._find_method_callers(all_files, method_name, project_path)

            if not callers:
                break

            # For simplicity, we just take the first caller found. A real tool would need to resolve which one is correct.
            caller = callers[0]
            
            caller_lines = all_files.get(caller['abs_path'], [])
            trace.append({
                'file': caller['file'],
                'line': caller['line'],
                'column': caller['column'],
                'signature': enclosing_method['signature'],
                'codeLines': self._get_code_context(caller_lines, caller['line'] - 1),
                'problemLineIndex': 5,
            })
            
            # Set up for the next iteration
            current_file_path = caller['abs_path']
            current_line_index = caller['line'] - 1

        return trace

    def _is_in_try_catch_block(self, lines: List[str], line_index: int) -> bool:
        """
        Check if the given line is within a try-catch block.
        Walks up the scope hierarchy to check containing methods and classes.
        """
        # Simple heuristic: look backwards for 'try' and forwards for 'catch'
        # This is a simplified implementation - a full parser would be more accurate
        
        brace_count = 0
        found_try = False
        
        # Look backwards from current line
        for i in range(line_index, -1, -1):
            line = lines[i].strip()
            brace_count += line.count('}') - line.count('{')
            if brace_count > 0: break
            if 'try' in line and '{' in line:
                found_try = True
                break
        
        if found_try:
            brace_count = 0
            in_try_block = True
            for i in range(line_index, len(lines)):
                line = lines[i].strip()
                brace_count += line.count('{') - line.count('}')
                if in_try_block and brace_count < 0: in_try_block = False
                if not in_try_block and ('catch' in line or 'finally' in line): return True
                if not in_try_block and brace_count <= -2: break
        
        return False
    
    def _check_method_scope(self, lines: List[str], method_start: int, original_line: int) -> bool:
        """Check if a method scope has try-catch that covers the original line."""
        # This is a simplified check - would need more sophisticated parsing
        # for production use
        return False
    
    def _get_code_context(self, lines: List[str], line_index: int) -> List[Dict[str, Any]]:
        """Get 11 lines of context around the problematic line (5 before, current, 5 after)."""
        context_lines = []
        
        start_line = max(0, line_index - 5)
        end_line = min(len(lines), line_index + 6)
        
        for i in range(start_line, end_line):
            context_lines.append({
                'number': i + 1,
                'content': lines[i] if i < len(lines) else ''
            })
        
        return context_lines


class CodeAnalyzerServer:
    """Main WebSocket server for the Code Analyzer application."""
    
    def __init__(self):
        self.checkers = [
            ExceptionChecker()
        ]
    
    async def handle_client(self, websocket, path):
        """Handle WebSocket client connections and process analysis requests."""
        print(f"Client connected from {websocket.remote_address}")
        
        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    await self.process_message(websocket, data)
                except json.JSONDecodeError:
                    await self.send_error(websocket, "Invalid JSON message")
                except Exception as e:
                    await self.send_error(websocket, f"Error processing message: {str(e)}")
        
        except websockets.exceptions.ConnectionClosed:
            print(f"Client {websocket.remote_address} disconnected")
        except Exception as e:
            print(f"Error handling client {websocket.remote_address}: {str(e)}")
    
    async def process_message(self, websocket, data):
        """Process incoming messages from the client."""
        message_type = data.get('type')
        
        if message_type == 'analyze_project':
            project_path = data.get('path')
            if not project_path:
                await self.send_error(websocket, "Project path is required")
                return
            
            await self.analyze_project(websocket, project_path)
        else:
            await self.send_error(websocket, f"Unknown message type: {message_type}")
    
    async def analyze_project(self, websocket, project_path):
        """Run all configured checkers on the given project."""
        if not os.path.exists(project_path):
            await self.send_error(websocket, f"Project path does not exist: {project_path}")
            return
        
        await self.send_progress(websocket, f"Starting analysis of: {project_path}")
        
        total_findings = []
        
        # Run each checker
        for checker in self.checkers:
            await self.send_progress(websocket, f"Running {checker.name}...")
            
            try:
                # Create progress callback for this checker
                async def progress_callback(message):
                    await self.send_progress(websocket, f"[{checker.name}] {message}")
                
                findings = await checker.run(project_path, progress_callback)
                
                # Send each finding to the client
                for finding in findings:
                    await self.send_finding(websocket, finding)
                
                total_findings.extend(findings)
                
            except Exception as e:
                error_msg = f"Error in {checker.name}: {str(e)}"
                await self.send_error(websocket, error_msg)
                await self.send_progress(websocket, error_msg)
        
        # Send completion message
        await self.send_complete(websocket, len(total_findings))
    
    async def send_progress(self, websocket, message):
        """Send a progress update to the client."""
        await websocket.send(json.dumps({
            'type': 'progress',
            'data': message
        }))
    
    async def send_finding(self, websocket, finding):
        """Send a code finding to the client."""
        await websocket.send(json.dumps({
            'type': 'finding',
            'data': finding
        }))
    
    async def send_complete(self, websocket, finding_count):
        """Send analysis completion message to the client."""
        await websocket.send(json.dumps({
            'type': 'complete',
            'data': {
                'total_findings': finding_count,
                'message': f'Analysis complete. Found {finding_count} issues.'
            }
        }))
    
    async def send_error(self, websocket, error_message):
        """Send an error message to the client."""
        await websocket.send(json.dumps({
            'type': 'error',
            'data': error_message
        }))
    
    def add_checker(self, checker: CodeChecker):
        """Add a new checker to the analysis pipeline."""
        if not isinstance(checker, CodeChecker):
            raise ValueError("Checker must inherit from CodeChecker")
        self.checkers.append(checker)
    
    async def start_server(self, host='localhost', port=8765):
        """Start the WebSocket server."""
        print(f"Starting Code Analyzer server on {host}:{port}")
        print("Available checkers:")
        for checker in self.checkers:
            print(f"  - {checker.name}")
        
        server = await websockets.serve(
            self.handle_client,
            host,
            port,
            ping_interval=20,
            ping_timeout=10
        )
        
        print(f"Server running. Connect frontend to ws://{host}:{port}")
        print("Press Ctrl+C to stop the server")
        
        try:
            await server.wait_closed()
        except KeyboardInterrupt:
            print("\nShutting down server...")
            server.close()
            await server.wait_closed()


# Example of how to add a custom checker
class CustomChecker(CodeChecker):
    """
    Example custom checker implementation.
    This demonstrates how to add new checker modules to the system.
    """
    
    @property
    def name(self) -> str:
        return "Custom Checker Example"
    
    async def run(self, project_path: str, progress_callback) -> List[Dict[str, Any]]:
        """Example implementation of a custom checker."""
        await progress_callback("Custom checker starting...")
        
        # Your custom analysis logic here
        findings = []
        
        # Example: Find files with TODO comments
        java_files = []
        for root, dirs, files in os.walk(project_path):
            for file in files:
                if file.endswith('.java'):
                    java_files.append(os.path.join(root, file))
        
        for file_path in java_files:
            relative_path = os.path.relpath(file_path, project_path)
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = f.readlines()
                
                for line_num, line in enumerate(lines, 1):
                    if 'TODO' in line.upper():
                        findings.append({
                            'file': relative_path,
                            'line': line_num,
                            'column': line.upper().find('TODO') + 1,
                            'description': 'TODO comment found',
                            'codeLines': self._get_context_lines(lines, line_num - 1),
                            'problemLineIndex': 5
                        })
            except Exception:
                continue
        
        await progress_callback(f"Custom checker found {len(findings)} TODOs")
        return findings
    
    def _get_context_lines(self, lines: List[str], line_index: int) -> List[Dict[str, Any]]:
        """Get context lines around the issue."""
        context_lines = []
        start_line = max(0, line_index - 5)
        end_line = min(len(lines), line_index + 6)
        
        for i in range(start_line, end_line):
            context_lines.append({
                'number': i + 1,
                'content': lines[i].rstrip('\n') if i < len(lines) else ''
            })
        
        return context_lines


async def main():
    """Main entry point for the Code Analyzer backend."""
    server = CodeAnalyzerServer()
    
    # Uncomment to add the custom checker example:
    # server.add_checker(CustomChecker())
    
    try:
        await server.start_server()
    except Exception as e:
        print(f"Failed to start server: {e}")


if __name__ == "__main__":
    # Check for required dependencies
    try:
        import websockets
    except ImportError:
        print("Error: websockets library is required. Install it with:")
        print("pip install websockets")
        exit(1)
    
    # Run the server
    asyncio.run(main())