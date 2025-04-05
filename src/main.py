from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse
import os
import asyncio
import aiofiles
from loguru import logger

app = FastAPI()

VIDEO_PATH = "videos/mock.mp4"
CHUNK_SIZE = 256 * 1024  # 256KB чанки
INITIAL_CHUNK_SIZE = 4 * 1024 * 1024  # 4MB для первого чанка

HTML_CONTENT = """
<!DOCTYPE html>
<html>
<head>
    <title>Video Streaming</title>
</head>
<body>
    <h2>HTTP Streaming</h2>
    <video src="/video" id="httpPlayer" controls width="640" height="360"></video>
    
    <h2>WebSocket Streaming</h2>
    <video id="wsPlayer" controls width="640" height="360"></video>

    <script>
        const video = document.getElementById("wsPlayer");
        const mimeCodec = 'video/mp4; codecs="avc1.42E01E,mp4a.40.2"';
        
        let mediaSource = null;
        let sourceBuffer = null;
        let queue = [];
        let ws = null;
        let isSourceBufferReady = false;
        let retryCount = 0;
        const MAX_RETRIES = 3;

        function resetPlayer() {
            if (sourceBuffer && mediaSource) {
                try {
                    if (sourceBuffer.updating) {
                        sourceBuffer.abort();
                    }
                    mediaSource.removeSourceBuffer(sourceBuffer);
                } catch (e) {
                    console.log('Ошибка удаления SourceBuffer:', e);
                }
            }
            if (mediaSource) {
                try {
                    if (mediaSource.readyState === 'open') {
                        mediaSource.endOfStream();
                    }
                } catch (e) {
                    console.log('Ошибка закрытия MediaSource:', e);
                }
            }
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.close();
            }
            
            queue = [];
            sourceBuffer = null;
            mediaSource = null;
            isSourceBufferReady = false;
            video.src = '';
        }

        async function initMediaSource() {
            return new Promise((resolve, reject) => {
                resetPlayer();
                
                mediaSource = new MediaSource();
                video.src = URL.createObjectURL(mediaSource);

                const sourceOpenHandler = () => {
                    try {
                        if (!mediaSource || mediaSource.readyState !== 'open') {
                            throw new Error('MediaSource не открыт');
                        }

                        sourceBuffer = mediaSource.addSourceBuffer(mimeCodec);
                        sourceBuffer.mode = 'segments';
                        isSourceBufferReady = true;
                        
                        sourceBuffer.addEventListener('updateend', () => {
                            if (queue.length > 0 && !sourceBuffer.updating && isSourceBufferReady) {
                                try {
                                    const chunk = queue.shift();
                                    if (sourceBuffer.buffered.length > 0) {
                                        const bufferedEnd = sourceBuffer.buffered.end(sourceBuffer.buffered.length - 1);
                                        if (bufferedEnd > 10) { // Если в буфере больше 10 секунд
                                            sourceBuffer.remove(0, bufferedEnd - 5); // Оставляем последние 5 секунд
                                        }
                                    }
                                    sourceBuffer.appendBuffer(chunk);
                                } catch (e) {
                                    console.error('Ошибка добавления из очереди:', e);
                                    handleError(e);
                                }
                            }
                        });

                        sourceBuffer.addEventListener('error', (e) => {
                            console.error('Ошибка SourceBuffer:', e);
                            handleError(e);
                        });

                        sourceBuffer.addEventListener('abort', (e) => {
                            console.error('SourceBuffer прерван:', e);
                            handleError(e);
                        });
                        
                        resolve();
                    } catch (error) {
                        console.error('Ошибка инициализации SourceBuffer:', error);
                        reject(error);
                    }
                };

                mediaSource.addEventListener('sourceopen', sourceOpenHandler, { once: true });
                
                mediaSource.addEventListener('sourceended', () => {
                    console.log('MediaSource закончил работу');
                    isSourceBufferReady = false;
                });

                mediaSource.addEventListener('sourceclose', () => {
                    console.log('MediaSource закрыт');
                    isSourceBufferReady = false;
                });

                // Таймаут для инициализации
                setTimeout(() => {
                    if (!isSourceBufferReady) {
                        reject(new Error('Таймаут инициализации MediaSource'));
                    }
                }, 5000);
            });
        }

        function handleError(error) {
            console.error('Произошла ошибка:', error);
            if (retryCount < MAX_RETRIES) {
                retryCount++;
                console.log(`Попытка переподключения ${retryCount}/${MAX_RETRIES}`);
                resetPlayer();
                setTimeout(startStreaming, 1000 * retryCount);
            } else {
                console.error('Превышено максимальное количество попыток переподключения');
                resetPlayer();
            }
        }

        async function startStreaming() {
            try {
                await initMediaSource();
                
                ws = new WebSocket('ws://localhost:8000/ws');
                
                ws.onmessage = async (event) => {
                    if (event.data instanceof Blob) {
                        try {
                            const chunk = await event.data.arrayBuffer();
                            if (!isSourceBufferReady || !sourceBuffer) {
                                console.warn('SourceBuffer не готов, пропуск чанка');
                                return;
                            }
                            
                            if (sourceBuffer.updating || queue.length > 0) {
                                queue.push(chunk);
                            } else {
                                try {
                                    sourceBuffer.appendBuffer(chunk);
                                } catch (e) {
                                    console.error('Ошибка добавления буфера:', e);
                                    handleError(e);
                                }
                            }
                        } catch (e) {
                            console.error('Ошибка обработки chunk:', e);
                            handleError(e);
                        }
                    }
                };

                ws.onopen = () => {
                    console.log('WebSocket подключен');
                    retryCount = 0; // Сброс счетчика попыток при успешном подключении
                    ws.send(JSON.stringify({ action: 'start_stream' }));
                };

                ws.onerror = (error) => {
                    console.error('WebSocket ошибка:', error);
                    handleError(error);
                };

                ws.onclose = () => {
                    console.log('WebSocket закрыт');
                    handleError(new Error('WebSocket закрыт'));
                };

            } catch (error) {
                console.error('Ошибка инициализации:', error);
                handleError(error);
            }
        }

        // Запускаем стриминг при загрузке страницы
        startStreaming();
    </script>
</body>
</html>
                        """

@app.get("/")
async def root():
    return HTMLResponse(HTML_CONTENT)

@app.get("/video")
async def video_stream(request: Request):
    file_size = os.path.getsize(VIDEO_PATH)
    range_header = request.headers.get("range")

    if range_header:
        byte1, byte2 = 0, None
        m = range_header.strip().split("=")[-1]
        if '-' in m:
            parts = m.split("-")
            if parts[0]:
                byte1 = int(parts[0])
            if parts[1]:
                byte2 = int(parts[1])
        byte2 = byte2 or file_size - 1
        length = byte2 - byte1 + 1
        with open(VIDEO_PATH, "rb") as f:
            f.seek(byte1)
            data = f.read(length)
        return Response(
            content=data,
            status_code=206,
            headers={
                "Content-Range": f"bytes {byte1}-{byte2}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
                "Content-Type": "video/mp4",
            },
        )
    else:
        return StreamingResponse(open(VIDEO_PATH, "rb"), media_type="video/mp4")

@app.websocket("/socket-video")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    
    try:
        while True:
            data = await websocket.receive_json()
            
            if data.get('action') == 'start_stream':
                try:
                    async with aiofiles.open(VIDEO_PATH, 'rb') as video:
                        # Отправляем первый большой чанк для инициализации
                        first_chunk = await video.read(INITIAL_CHUNK_SIZE)
                        await websocket.send_bytes(first_chunk)
                        logger.info("Отправлен первый большой чанк")
                        await asyncio.sleep(1.0)  # Увеличенное время на инициализацию

                        # Отправляем остальное видео чанками
                        while True:
                            chunk = await video.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            await websocket.send_bytes(chunk)
                            logger.info(f"Отправлен чанк len: {len(chunk)}")
                            await asyncio.sleep(0.05)  # Уменьшенная задержка между чанками
                        logger.info("Стриминг завершен")
                        await websocket.send_bytes(b"END")
                except Exception as e:
                    logger.error(f"Ошибка стриминга: {str(e)}")
                    await websocket.close()
                    break

    except WebSocketDisconnect:
        logger.info("Клиент отключился")
    except Exception as e:
        logger.error(f"Ошибка WebSocket: {str(e)}")
        await websocket.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
