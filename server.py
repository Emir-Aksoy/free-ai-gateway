"""AI Gateway HTTP 服务。

对外提供 OpenAI 兼容接口，内部按语义模式路由到多个免费 provider。

本轮改动：
  - 单线程 HTTPServer 换成 ThreadingHTTPServer，一个慢请求不再堵死整个网关；
  - 支持 stream=true 的 SSE 透传；
  - 管理面整体移到 manage.py（经 SSH 调用），HTTP 不再暴露任何管理端点；
  - 访问日志只记脱敏后的客户端密钥与其短标识，不再记完整密钥；
  - 客户端参数（temperature/max_tokens/tools 等）原样转发，不再丢弃；
  - code/writing/agent 这类任务模式可以直接当 model 传入，原先会 KeyError 打崩连接；
  - 失败响应改成 OpenAI 错误格式，并对请求处理做异常隔离。
"""

import json
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from router.router import AIRouter
from router.recovery import RecoveryWorker
from core.apikey import APIKeyManager, key_id, mask_key
from core.logger import GatewayLogger
from providers.base import usage_tokens

LISTEN_HOST = os.environ.get("GATEWAY_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("GATEWAY_PORT", "8090"))

router = AIRouter()
key_manager = APIKeyManager()
logger = GatewayLogger()

# 任务型模式：既可以 model="task" + task_type=code，也可以直接 model="code"
TASK_MODES = ("code", "writing", "agent")


class GatewayHandler(BaseHTTPRequestHandler):

    protocol_version = "HTTP/1.1"
    server_version = "ai-gateway/2.0"

    # ---------- 响应工具 ----------

    def _send(self, status, body, content_type="application/json"):
        payload = body if isinstance(body, bytes) else body.encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, data, status=200):
        self._send(status, json.dumps(data, ensure_ascii=False))

    def _write_chunked(self, data):
        """按 HTTP/1.1 分块传输编码写出一块并立即刷出。

        BaseHTTPRequestHandler 不会自动做 chunked，声明了这个头就得自己编码：
        每块为 <十六进制长度>CRLF<数据>CRLF。
        """

        if not data:
            return

        self.wfile.write(b"%X\r\n" % len(data))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _end_chunked(self):
        """长度为 0 的结束块，客户端据此判定响应结束。"""

        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _error(self, message, status=400, code=None, extra=None):
        """OpenAI 错误格式。客户端 SDK 依赖这个结构判断失败原因，
        原先直接回 {"success": false, "errors": [...]} 客户端无法解析。"""

        error = {
            "message": message,
            "type": "invalid_request_error" if status < 500 else "api_error",
            "code": code,
        }

        if extra:
            error["gateway_errors"] = extra

        self._json({"error": error}, status)

    # ---------- 鉴权 ----------

    def _bearer(self):
        auth = self.headers.get("Authorization", "")

        if not auth.startswith("Bearer "):
            return None

        return auth.split(" ", 1)[1].strip()

    def _require_client_key(self):
        api_key = self._bearer()

        if not api_key or not key_manager.verify(api_key):
            self._error(
                "Invalid API key", 401, "invalid_api_key"
            )
            return None

        return api_key

    # ---------- GET ----------

    def do_GET(self):
        try:
            self._route_get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._safe_fail(e)

    def _route_get(self):
        path = self.path.split("?", 1)[0]

        if path == "/health":
            self._json({"status": "ok", "service": "ai-gateway"})
            return

        if path == "/v1/models":
            if not self._require_client_key():
                return

            self._json(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": name,
                            "object": "model",
                            "created": 0,
                            "owned_by": "ai-gateway",
                        }
                        for name in self._available_modes()
                    ],
                }
            )
            return

        self._error("Not found", 404, "not_found")

    def _available_modes(self):
        names = []

        for name, value in router.modes.items():
            if name == "task":
                names.extend(sorted(value.keys()))
            else:
                names.append(name)

        return names

    # ---------- POST ----------

    def do_POST(self):
        try:
            self._route_post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._safe_fail(e)

    def _safe_fail(self, exc):
        """任何未预期异常都不该让连接直接断掉——客户端会看到 HTTP 000。"""

        try:
            logger.error("handler error: %s: %s" % (type(exc).__name__, exc))
        except Exception:
            pass

        try:
            self._error(
                "Internal gateway error: %s" % type(exc).__name__,
                500,
                "internal_error",
            )
        except Exception:
            pass

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0

        if length <= 0:
            return {}

        if length > 8 * 1024 * 1024:
            raise ValueError("请求体过大")

        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _route_post(self):
        # One handler can serve multiple requests on a keep-alive connection.
        for field in ("_responses_custom", "_responses_model", "_configured_order"):
            if hasattr(self, field): delattr(self, field)
        path = self.path.split("?", 1)[0]

        if path not in ("/v1/chat/completions", "/v1/responses"):
            self._error("Not found", 404, "not_found")
            return

        api_key = self._require_client_key()

        if not api_key:
            return

        try:
            body = self._read_json_body()
        except ValueError as e:
            self._error("Invalid JSON body: %s" % e, 400, "invalid_body")
            return

        if path == "/v1/responses":
            from core.responses import to_chat_request
            try:
                request, custom = to_chat_request(body)
            except ValueError as error:
                self._error(str(error), 400, "unsupported_responses_request")
                return
            self._responses_custom = custom
            self._responses_model = request["model"]
            original_body, body = body, request
        else:
            original_body = body
        messages = body.get("messages")

        if not isinstance(messages, list) or not messages:
            self._error("messages 不能为空", 400, "invalid_messages")
            return

        mode = body.get("model") or "fast"
        task_type = body.get("task_type")
        if "gateway_route_order" in body:
            if (path != "/v1/chat/completions" or mode != "thinking" or body.get("stream")
                    or body["gateway_route_order"] != "configured"):
                self._error("gateway_route_order 仅支持thinking非流式configured请求", 400, "invalid_route_order")
                return
            self._configured_order = True

        # 直接把 code/writing/agent 当 model 传进来时，补上 task_type，
        # 让 capability 打分用对应的权重
        if not task_type and mode in TASK_MODES:
            task_type = mode

        params = {
            key: value
            for key, value in body.items()
            if key not in ("model", "messages", "stream", "task_type", "gateway_route_order")
        }

        if body.get("stream"):
            self._handle_stream(api_key, mode, messages, task_type, params, request_body=original_body)
        else:
            self._handle_chat(api_key, mode, messages, task_type, params, request_body=original_body)

    def _handle_chat(self, api_key, mode, messages, task_type, params, request_body=None):
        options = {}
        if getattr(self, "_configured_order", False):
            options["preserve_order"] = True
            def require_text(payload):
                try:
                    text = payload["choices"][0]["message"].get("content")
                except (TypeError, KeyError, IndexError, AttributeError):
                    text = None
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("助手需要非空文字回答")
            options["response_validator"] = require_text
        if hasattr(self, "_responses_custom"):
            from core.responses import from_chat
            options["response_validator"] = lambda payload: from_chat(payload, self._responses_model, self._responses_custom)
        result = router.chat(
            mode=mode, messages=messages, task_type=task_type, params=params, request_body=request_body, **options
        )

        key_manager.record(api_key)

        if not result.get("success"):
            logger.access(
                {
                    "event": "request_failed",
                    "key": mask_key(api_key),
                    "key_id": key_id(api_key),
                    "mode": mode,
                    "success": False,
                    "errors": result.get("errors", []),
                }
            )

            self._error(
                "所有候选模型均不可用",
                502,
                "all_providers_failed",
                extra=result.get("errors", []),
            )
            return

        payload = result.get("result") or {}
        tokens = usage_tokens(payload)

        if hasattr(self, "_responses_custom"):
            from core.responses import from_chat
            try:
                payload = from_chat(payload, self._responses_model, self._responses_custom)
            except ValueError:
                self._error("Upstream response is incompatible with Responses API", 502, "invalid_response")
                return
        logger.access(
            {
                "event": "request_success",
                "key": mask_key(api_key),
                "key_id": key_id(api_key),
                "provider": result.get("provider"),
                "model": result.get("model"),
                "latency": result.get("latency"),
                "tokens": tokens,
                "success": True,
            }
        )

        if getattr(self, "_configured_order", False):
            from core.assistant import safe_errors
            payload = dict(payload, _gateway={"target": result.get("target"),
                                             "errors": safe_errors(result.get("errors"))})
        self._json(payload)

    def _handle_stream(self, api_key, mode, messages, task_type, params, request_body=None):
        adapter = None
        options = {}
        if hasattr(self, "_responses_custom"):
            from core.responses import ResponseStream
            adapter = ResponseStream(self._responses_model, self._responses_custom)
            options["response_adapter"] = adapter
        meta, chunks = router.stream(
            mode=mode, messages=messages, task_type=task_type, params=params, request_body=request_body, **options
        )

        key_manager.record(api_key)

        if not meta.get("success"):
            logger.access(
                {
                    "event": "request_failed",
                    "key": mask_key(api_key),
                    "key_id": key_id(api_key),
                    "mode": mode,
                    "stream": True,
                    "success": False,
                    "errors": meta.get("errors", []),
                }
            )

            self._error(
                "所有候选模型均不可用",
                502,
                "all_providers_failed",
                extra=meta.get("errors", []),
            )
            return

        # 头部一旦发出就不能再改状态码，所以降级必须在这之前完成。
        # HTTP/1.1 下响应必须能界定长度：流式没有 Content-Length，
        # 就必须走 chunked，否则客户端读到最后一块也不知道结束了，会一直等到超时。
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        sent = 0
        tokens = 0
        def send_event(event):
            self._write_chunked(("event: %s\ndata: %s\n\n" % (event["type"], json.dumps(event, ensure_ascii=False))).encode("utf-8"))

        try:
            if adapter:
                for event in adapter.start_events(): send_event(event)
            for chunk in chunks:
                try:
                    parsed = json.loads(chunk)
                    tokens = usage_tokens(parsed) or tokens
                except ValueError:
                    pass

                if adapter:
                    send_event(json.loads(chunk))
                else:
                    self._write_chunked(("data: %s\n\n" % chunk).encode("utf-8"))
                sent += 1

            if not adapter:
                self._write_chunked(b"data: [DONE]\n\n")
            self._end_chunked()

        except (BrokenPipeError, ConnectionResetError):
            # 客户端提前断开是正常现象，不算错误。
            # 但这条连接已经写坏且缺少结束块，不能再留给下一个请求复用。
            self.close_connection = True

            logger.access(
                {
                    "event": "stream_client_abort",
                    "key": mask_key(api_key),
                    "key_id": key_id(api_key),
                    "provider": meta.get("provider"),
                    "model": meta.get("model"),
                    "chunks": sent,
                }
            )
            return

        except Exception:
            logger.access({"event": "request_failed", "key": mask_key(api_key), "key_id": key_id(api_key),
                           "provider": meta.get("provider"), "model": meta.get("model"),
                           "stream": True, "success": False, "code": "upstream_stream_error"})
            try:
                if adapter:
                    adapter.response["status"] = "failed"
                    adapter.response["error"] = {"code": "upstream_stream_error", "message": "Upstream stream failed"}
                    send_event(adapter.event("response.failed", response=adapter.response))
                else:
                    self._write_chunked(b'data: {"error":{"message":"Upstream stream failed","code":"upstream_stream_error"}}\n\ndata: [DONE]\n\n')
                self._end_chunked()
            except Exception:
                self.close_connection = True
            return

        finally:
            # 客户端中途断开时生成器不会耗尽，只靠 GC 回收会让上游连接
            # 在池里多滞留一段时间。显式关闭，让 provider 那层的
            # finally: response.close() 立刻执行。finally 覆盖正常结束、
            # 断连、异常三条路径。
            closer = getattr(chunks, "close", None)

            if closer:
                try:
                    closer()
                except Exception:
                    pass

        if adapter and adapter.response.get("usage"):
            tokens = adapter.response["usage"].get("total_tokens", tokens)
        logger.access(
            {
                "event": "request_success",
                "key": mask_key(api_key),
                "key_id": key_id(api_key),
                "provider": meta.get("provider"),
                "model": meta.get("model"),
                "latency": meta.get("latency"),
                "tokens": tokens,
                "chunks": sent,
                "stream": True,
                "success": True,
            }
        )

    def log_message(self, fmt, *args):
        return


class Gateway(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, *args, **kwargs):
        maximum = int(os.environ.get("GATEWAY_MAX_CONNECTIONS", "0"))
        timeout = float(os.environ.get("GATEWAY_IDLE_TIMEOUT", "0"))
        self.connection_slots = threading.BoundedSemaphore(maximum) if maximum > 0 else None
        self.idle_timeout = timeout if timeout > 0 else None
        super().__init__(*args, **kwargs)

    def get_request(self):
        request, address = super().get_request()
        if self.idle_timeout is not None:
            request.settimeout(self.idle_timeout)
        return request, address

    def process_request(self, request, client_address):
        if self.connection_slots is not None and not self.connection_slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nRetry-After: 1\r\nContent-Length: 0\r\n\r\n')
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            if self.connection_slots is not None:
                self.connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            if self.connection_slots is not None:
                self.connection_slots.release()


def flush_all():
    try:
        router.state.flush()
        router.quota.flush()
        router.save_cooldowns()
        key_manager.flush()
    except Exception:
        pass


def run():
    from core.usage import enable_usage
    enable_usage()
    server = Gateway((LISTEN_HOST, LISTEN_PORT), GatewayHandler)
    recovery = RecoveryWorker(router, logger)

    def shutdown(signum, frame):
        flush_all()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print(
        "AI Gateway listening on %s:%d" % (LISTEN_HOST, LISTEN_PORT),
        flush=True,
    )

    recovery.start()
    try:
        server.serve_forever()
    finally:
        recovery.stop()
        flush_all()
        server.server_close()


if __name__ == "__main__":
    run()
