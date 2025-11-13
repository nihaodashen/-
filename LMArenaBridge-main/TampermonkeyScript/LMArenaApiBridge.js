// ==UserScript==
// @name         LMArena API Bridge
// @namespace    http://tampermonkey.net/
// @version      2.5
// @description  Bridges LMArena to a local API server via WebSocket for streamlined automation.
// @author       Lianues
// @match        https://lmarena.ai/*
// @match        https://*.lmarena.ai/*
// @icon         https://www.google.com/s2/favicons?sz=64&domain=lmarena.ai
// @grant        none
// @run-at       document-end
// ==/UserScript==

(function () {
    'use strict';

    // --- 配置 ---
    const SERVER_URL = "ws://localhost:5102/ws"; // 与 api_server.py 中的端口匹配
    let socket;
    let isCaptureModeActive = false; // ID捕获模式的开关

    // --- 核心逻辑 ---
    function connect() {
        console.log(`[API Bridge] 正在连接到本地服务器: ${SERVER_URL}...`);
        socket = new WebSocket(SERVER_URL);

        socket.onopen = () => {
            console.log("[API Bridge] ✅ 与本地服务器的 WebSocket 连接已建立。");
            document.title = "✅ " + document.title;
        };

        socket.onmessage = async (event) => {
            try {
                const message = JSON.parse(event.data);

                // 检查是否是指令，而不是标准的聊天请求
                if (message.command) {
                    console.log(`[API Bridge] ⬇️ 收到指令: ${message.command}`);
                    if (message.command === 'refresh' || message.command === 'reconnect') {
                        console.log(`[API Bridge] 收到 '${message.command}' 指令，正在执行页面刷新...`);
                        location.reload();
                    } else if (message.command === 'activate_id_capture') {
                        console.log("[API Bridge] ✅ ID 捕获模式已激活。请在页面上触发一次 'Retry' 操作。");
                        isCaptureModeActive = true;
                        // 可以选择性地给用户一个视觉提示
                        document.title = "🎯 " + document.title;
                    } else if (message.command === 'send_page_source') {
                       console.log("[API Bridge] 收到发送页面源码的指令，正在发送...");
                       sendPageSource();
                    }
                    return;
                }

                const { request_id, payload } = message;

                if (!request_id || !payload) {
                    console.error("[API Bridge] 收到来自服务器的无效消息:", message);
                    return;
                }
                
                console.log(`[API Bridge] ⬇️ 收到聊天请求 ${request_id.substring(0, 8)}。准备执行 fetch 操作。`);
                await executeFetchAndStreamBack(request_id, payload);

            } catch (error) {
                console.error("[API Bridge] 处理服务器消息时出错:", error);
            }
        };

        socket.onclose = () => {
            console.warn("[API Bridge] 🔌 与本地服务器的连接已断开。将在5秒后尝试重新连接...");
            if (document.title.startsWith("✅ ")) {
                document.title = document.title.substring(2);
            }
            setTimeout(connect, 5000);
        };

        socket.onerror = (error) => {
            console.error("[API Bridge] ❌ WebSocket 发生错误:", error);
            socket.close(); // 会触发 onclose 中的重连逻辑
        };
    }

    // UUID v7 Generator - Time-ordered UUID
    function generateUUIDv7() {
        // Get current timestamp in milliseconds
        const timestamp = Date.now();

        // Generate random bytes for the rest of the UUID
        const randomBytes = new Uint8Array(10);
        crypto.getRandomValues(randomBytes);

        // Convert timestamp to hex (48 bits / 6 bytes)
        const timestampHex = timestamp.toString(16).padStart(12, '0');

        // Build UUID v7 format: xxxxxxxx-xxxx-7xxx-yxxx-xxxxxxxxxxxx
        // where x is timestamp or random, 7 is version, y is variant (8, 9, a, or b)

        // First 8 hex chars (32 bits) from timestamp
        const part1 = timestampHex.substring(0, 8);

        // Next 4 hex chars (16 bits) from timestamp
        const part2 = timestampHex.substring(8, 12);

        // Version (4 bits = 7) + 12 bits random
        const part3 = '7' + Array.from(randomBytes.slice(0, 2))
            .map(b => b.toString(16).padStart(2, '0'))
            .join('')
            .substring(1, 4);

        // Variant (2 bits = 10b) + 14 bits random
        const variant = (randomBytes[2] & 0x3f) | 0x80; // Set variant bits to 10xxxxxx
        const part4 = variant.toString(16).padStart(2, '0') +
            randomBytes[3].toString(16).padStart(2, '0');

        // Last 48 bits (12 hex chars) random
        const part5 = Array.from(randomBytes.slice(4, 10))
            .map(b => b.toString(16).padStart(2, '0'))
            .join('');

        return `${part1}-${part2}-${part3}-${part4}-${part5}`;
    }

    async function executeFetchAndStreamBack(requestId, payload) {
        console.log(`[API Bridge] 当前操作域名: ${window.location.hostname}`);
        const { is_image_request, message_templates, target_model_id, session_id, battle_target } = payload;

        // --- 使用从后端配置传递的会话信息 ---
        if (!session_id) {
            const errorMsg = "从后端收到的会话信息 (session_id) 为空。请先运行 `id_updater.py` 脚本进行设置。";
            console.error(`[API Bridge] ${errorMsg}`);
            sendToServer(requestId, { error: errorMsg });
            sendToServer(requestId, "[DONE]");
            return;
        }

        // 新的 URL 格式
        const apiUrl = `/nextjs-api/stream/post-to-evaluation/${session_id}`;
        const httpMethod = 'POST';
        
        console.log(`[API Bridge] 使用 API 端点: ${apiUrl}`);
        console.log(`[API Bridge] Battle 目标位置: ${battle_target || 'b'}`);
        
        if (!message_templates || message_templates.length === 0) {
            const errorMsg = "从后端收到的消息列表为空。";
            console.error(`[API Bridge] ${errorMsg}`);
            sendToServer(requestId, { error: errorMsg });
            sendToServer(requestId, "[DONE]");
            return;
        }

        // 生成所需的 ID
        const userMessageId = generateUUIDv7();
        const modelAMessageId = generateUUIDv7();
        const modelBMessageId = generateUUIDv7();

        // 构建消息数组
        const newMessages = [];
        for (let i = 0; i < message_templates.length; i++) {
            const template = message_templates[i];
            const messageId = generateUUIDv7();
            
            newMessages.push({
                id: messageId,
                evaluationSessionId: session_id,
                role: template.role,
                parentMessageIds: [],
                content: template.content,
                experimental_attachments: Array.isArray(template.attachments) ? template.attachments : [],
                participantPosition: template.participantPosition || "b",
            });
        }

        // 构建新的请求体结构
        const body = {
            id: session_id,
            mode: "battle",
            userMessageId: userMessageId,
            modelAMessageId: modelAMessageId,
            modelBMessageId: modelBMessageId,
            messages: newMessages,
            modality: "chat"
        };

        console.log("[API Bridge] 准备发送到 LMArena API 的最终载荷:", JSON.stringify(body, null, 2));

        // 设置一个标志，让我们的 fetch 拦截器知道这个请求是脚本自己发起的
        window.isApiBridgeRequest = true;
        try {
            const response = await fetch(apiUrl, {
                method: httpMethod,
                headers: {
                    'Content-Type': 'text/plain;charset=UTF-8', // LMArena 使用 text/plain
                    'Accept': '*/*',
                },
                body: JSON.stringify(body),
                credentials: 'include' // 必须包含 cookie
            });

            if (!response.ok || !response.body) {
                const errorBody = await response.text();
                throw new Error(`网络响应不正常。状态: ${response.status}. 内容: ${errorBody}`);
            }

            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            
            // 确定目标位置（默认为 'b'）
            const targetPosition = battle_target || 'b';
            console.log(`[API Bridge] 开始过滤流数据，仅转发位置 '${targetPosition}' 的响应`);

            while (true) {
                const { value, done } = await reader.read();
                if (done) {
                    console.log(`[API Bridge] ✅ 请求 ${requestId.substring(0, 8)} 的流已成功结束。`);
                    // 仅在流成功结束后发送 [DONE]
                    sendToServer(requestId, "[DONE]");
                    break;
                }
                const chunk = decoder.decode(value);
                
                // 根据 battle_target 过滤数据块
                const filteredChunk = filterStreamByTarget(chunk, targetPosition);
                
                // 只有在有过滤后的内容时才发送
                if (filteredChunk) {
                    sendToServer(requestId, filteredChunk);
                }
            }

        } catch (error) {
            console.error(`[API Bridge] ❌ 在为请求 ${requestId.substring(0, 8)} 执行 fetch 时出错:`, error);
            // 发生错误时，只发送错误信息，不再发送 [DONE]
            sendToServer(requestId, { error: error.message });
        } finally {
            // 请求结束后，无论成功与否，都重置标志
            window.isApiBridgeRequest = false;
        }
    }

    // 根据目标位置过滤流数据
    function filterStreamByTarget(chunk, targetPosition) {
        // 创建正则表达式匹配目标位置的数据
        // 例如：a0:"..." 或 ad:{...} 用于位置 'a'
        // 例如：b0:"..." 或 bd:{...} 用于位置 'b'
        const pattern = new RegExp(`${targetPosition}[0d]:[^\\n]*`, 'g');
        const matches = chunk.match(pattern);
        
        if (matches && matches.length > 0) {
            // 返回所有匹配项，用换行符连接
            return matches.join('\n') + '\n';
        }
        
        return null; // 没有匹配的内容
    }

    function sendToServer(requestId, data) {
        if (socket && socket.readyState === WebSocket.OPEN) {
            const message = {
                request_id: requestId,
                data: data
            };
            socket.send(JSON.stringify(message));
        } else {
            console.error("[API Bridge] 无法发送数据，WebSocket 连接未打开。");
        }
    }

    // --- 网络请求拦截 ---
    const originalFetch = window.fetch;
    window.fetch = function(...args) {
        const urlArg = args[0];
        let urlString = '';

        // 确保我们总是处理字符串形式的 URL
        if (urlArg instanceof Request) {
            urlString = urlArg.url;
        } else if (urlArg instanceof URL) {
            urlString = urlArg.href;
        } else if (typeof urlArg === 'string') {
            urlString = urlArg;
        }

        // 仅在 URL 是有效字符串时才进行匹配
        if (urlString) {
            const match = urlString.match(/\/nextjs-api\/stream\/retry-evaluation-session-message\/([a-f0-9-]+)\/messages\/([a-f0-9-]+)/);

            // 仅在请求不是由API桥自身发起，且捕获模式已激活时，才更新ID
            if (match && !window.isApiBridgeRequest && isCaptureModeActive) {
                const sessionId = match[1];
                console.log(`[API Bridge Interceptor] 🎯 在激活模式下捕获到 session ID！正在发送...`);

                // 关闭捕获模式，确保只发送一次
                isCaptureModeActive = false;
                if (document.title.startsWith("🎯 ")) {
                    document.title = document.title.substring(2);
                }

                // 异步将捕获到的ID发送到本地的 id_updater.py 脚本
                fetch('http://127.0.0.1:5103/update', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ sessionId })
                })
                .then(response => {
                    if (!response.ok) throw new Error(`Server responded with status: ${response.status}`);
                    console.log(`[API Bridge] ✅ Session ID 更新成功发送。捕获模式已自动关闭。`);
                })
                .catch(err => {
                    console.error('[API Bridge] 发送ID更新时出错:', err.message);
                    // 即使发送失败，捕获模式也已关闭，不会重试。
                });
            }
        }

        // 调用原始的 fetch 函数，确保页面功能不受影响
        return originalFetch.apply(this, args);
    };


    // --- 页面源码发送 ---
    async function sendPageSource() {
        try {
            const htmlContent = document.documentElement.outerHTML;
            await fetch('http://localhost:5102/internal/update_available_models', { // 新的端点
                method: 'POST',
                headers: {
                    'Content-Type': 'text/html; charset=utf-8'
                },
                body: htmlContent
            });
             console.log("[API Bridge] 页面源码已成功发送。");
        } catch (e) {
            console.error("[API Bridge] 发送页面源码失败:", e);
        }
    }

    // --- 启动连接 ---
    console.log("========================================");
    console.log("  LMArena API Bridge v2.5 正在运行。");
    console.log("  - 聊天功能已连接到 ws://localhost:5102");
    console.log("  - ID 捕获器将发送到 http://localhost:5103");
    console.log("========================================");
    
    connect(); // 建立 WebSocket 连接

})();
