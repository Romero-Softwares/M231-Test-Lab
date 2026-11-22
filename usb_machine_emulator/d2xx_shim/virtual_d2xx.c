#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <string.h>
#include <stdarg.h>

#define FT_OK 0
#define FT_INVALID_HANDLE 1
#define FT_DEVICE_NOT_FOUND 2
#define FT_DEVICE_NOT_OPENED 3
#define FT_INVALID_PARAMETER 6
#define FT_DEVICE_232R 5
#define FT_FLAGS_OPENED 1
#define FT_OPEN_BY_SERIAL_NUMBER 1
#define FT_OPEN_BY_DESCRIPTION 2
#define FT_PURGE_RX 1
#define FT_PURGE_TX 2

#define M231_MAGIC 0x3233314D
#define M231_HEADER_SIZE 64
#define M231_CAPACITY (64 * 1024)
#define M231_RX_OFFSET M231_HEADER_SIZE
#define M231_TX_OFFSET (M231_RX_OFFSET + M231_CAPACITY)

typedef struct _M231_SHARED {
    uint32_t Magic;
    uint32_t Version;
    uint32_t ServerPid;
    uint32_t ClientPid;
    uint32_t Active;
    uint32_t RxLength;
    uint32_t TxLength;
    uint32_t ReadCount;
    uint32_t WriteCount;
    uint64_t Heartbeat;
} M231_SHARED;

typedef struct _M231_HANDLE {
    HANDLE Mapping;
    HANDLE Mutex;
    M231_SHARED *Shared;
    DWORD ReadTimeout;
    BYTE LatencyTimer;
} M231_HANDLE;

static const wchar_t *MAPPING_NAME = L"Local\\M231VirtualD2xx";
static const wchar_t *MUTEX_NAME = L"Local\\M231VirtualD2xxMutex";
static const char *SERIAL = "A501GR6Y";
static const char *DESCRIPTION = "FT232R USB UART";

static void append_str(char *dst, size_t dstSize, size_t *pos, const char *src) {
    while (*src != '\0' && (*pos + 1) < dstSize) {
        dst[(*pos)++] = *src++;
    }
    dst[*pos] = '\0';
}

static void append_uint(char *dst, size_t dstSize, size_t *pos, unsigned long value, unsigned int base, int width) {
    char tmp[32];
    int idx = 0;
    const char *digits = "0123456789ABCDEF";

    if (base != 10 && base != 16) base = 10;

    do {
        unsigned long rem = value % base;
        tmp[idx++] = digits[rem];
        value /= base;
    } while (value > 0 && idx < (int)sizeof(tmp));

    while (idx < width && idx < (int)sizeof(tmp) - 1) {
        tmp[idx++] = '0';
    }

    while (idx > 0 && (*pos + 1) < dstSize) {
        dst[(*pos)++] = tmp[--idx];
    }
    dst[*pos] = '\0';
}

static void shim_log(const char *fmt, ...) {
    HANDLE file;
    SYSTEMTIME st;
    char line[512];
    size_t pos = 0;
    DWORD written = 0;
    const char *p = fmt;
    va_list args;

    line[0] = '\0';
    GetLocalTime(&st);

    append_str(line, sizeof(line), &pos, "[");
    append_uint(line, sizeof(line), &pos, (unsigned long)st.wHour, 10, 2);
    append_str(line, sizeof(line), &pos, ":");
    append_uint(line, sizeof(line), &pos, (unsigned long)st.wMinute, 10, 2);
    append_str(line, sizeof(line), &pos, ":");
    append_uint(line, sizeof(line), &pos, (unsigned long)st.wSecond, 10, 2);
    append_str(line, sizeof(line), &pos, ".");
    append_uint(line, sizeof(line), &pos, (unsigned long)st.wMilliseconds, 10, 3);
    append_str(line, sizeof(line), &pos, "][PID ");
    append_uint(line, sizeof(line), &pos, (unsigned long)GetCurrentProcessId(), 10, 0);
    append_str(line, sizeof(line), &pos, "] ");

    va_start(args, fmt);
    while (*p != '\0' && (pos + 1) < sizeof(line)) {
        if (*p == '%' && *(p + 1) != '\0') {
            p++;
            if (*p == '0' && *(p + 1) == '8' && (*(p + 2) == 'l' || *(p + 2) == 'L') && (*(p + 3) == 'X' || *(p + 3) == 'x')) {
                unsigned long v = va_arg(args, unsigned long);
                append_uint(line, sizeof(line), &pos, v, 16, 8);
                p += 3;
            } else if (*p == 'l' && (*(p + 1) == 'u' || *(p + 1) == 'U')) {
                unsigned long v = va_arg(args, unsigned long);
                append_uint(line, sizeof(line), &pos, v, 10, 0);
                p += 1;
            } else if (*p == 'd' || *p == 'i') {
                int v = va_arg(args, int);
                if (v < 0) {
                    append_str(line, sizeof(line), &pos, "-");
                    append_uint(line, sizeof(line), &pos, (unsigned long)(0 - (unsigned int)v), 10, 0);
                } else {
                    append_uint(line, sizeof(line), &pos, (unsigned long)v, 10, 0);
                }
            } else if (*p == 's') {
                const char *s = va_arg(args, const char *);
                append_str(line, sizeof(line), &pos, s ? s : "(null)");
            } else if (*p == 'p') {
                uintptr_t ptr = (uintptr_t)va_arg(args, void *);
                append_str(line, sizeof(line), &pos, "0x");
                append_uint(line, sizeof(line), &pos, (unsigned long)ptr, 16, 0);
            } else if (*p == '%') {
                append_str(line, sizeof(line), &pos, "%");
            } else {
                char ch[2];
                ch[0] = *p;
                ch[1] = '\0';
                append_str(line, sizeof(line), &pos, ch);
            }
        } else {
            char ch[2];
            ch[0] = *p;
            ch[1] = '\0';
            append_str(line, sizeof(line), &pos, ch);
        }
        p++;
    }
    va_end(args);

    append_str(line, sizeof(line), &pos, "\r\n");

    file = CreateFileA("m231_shim.log", FILE_APPEND_DATA,
                       FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                       OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file != INVALID_HANDLE_VALUE) {
        WriteFile(file, line, (DWORD)pos, &written, NULL);
        CloseHandle(file);
    }
}

static void shim_log_bytes(const char *label, const BYTE *buffer, DWORD count) {
    HANDLE file;
    SYSTEMTIME st;
    char line[1024];
    size_t pos = 0;
    DWORD written = 0;
    DWORD index;

    line[0] = '\0';
    GetLocalTime(&st);
    append_str(line, sizeof(line), &pos, "[");
    append_uint(line, sizeof(line), &pos, (unsigned long)st.wHour, 10, 2);
    append_str(line, sizeof(line), &pos, ":");
    append_uint(line, sizeof(line), &pos, (unsigned long)st.wMinute, 10, 2);
    append_str(line, sizeof(line), &pos, ":");
    append_uint(line, sizeof(line), &pos, (unsigned long)st.wSecond, 10, 2);
    append_str(line, sizeof(line), &pos, ".");
    append_uint(line, sizeof(line), &pos, (unsigned long)st.wMilliseconds, 10, 3);
    append_str(line, sizeof(line), &pos, "][PID ");
    append_uint(line, sizeof(line), &pos, (unsigned long)GetCurrentProcessId(), 10, 0);
    append_str(line, sizeof(line), &pos, "] ");
    append_str(line, sizeof(line), &pos, label);
    append_str(line, sizeof(line), &pos, " len=");
    append_uint(line, sizeof(line), &pos, (unsigned long)count, 10, 0);
    append_str(line, sizeof(line), &pos, " hex=");
    for (index = 0; index < count && index < 256 && (pos + 4) < sizeof(line); index++) {
        static const char digits[] = "0123456789ABCDEF";
        BYTE value = buffer[index];
        line[pos++] = digits[(value >> 4) & 0x0F];
        line[pos++] = digits[value & 0x0F];
        if (index + 1 < count && index + 1 < 256) line[pos++] = ' ';
    }
    append_str(line, sizeof(line), &pos, "\r\n");
    file = CreateFileA("m231_shim.log", FILE_APPEND_DATA,
                       FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                       OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file != INVALID_HANDLE_VALUE) {
        WriteFile(file, line, (DWORD)pos, &written, NULL);
        CloseHandle(file);
    }
}

static BOOL process_is_alive(DWORD pid) {
    HANDLE process;
    DWORD result;
    if (pid == 0) return FALSE;
    process = OpenProcess(SYNCHRONIZE, FALSE, pid);
    if (process == NULL) return FALSE;
    result = WaitForSingleObject(process, 0);
    CloseHandle(process);
    return result == WAIT_TIMEOUT;
}

static BOOL mapping_available(void) {
    HANDLE mapping = OpenFileMappingW(FILE_MAP_READ, FALSE, MAPPING_NAME);
    M231_SHARED *shared;
    BOOL available = FALSE;
    if (mapping == NULL) return FALSE;
    shared = (M231_SHARED *)MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, M231_HEADER_SIZE);
    if (shared != NULL) {
        available = shared->Magic == M231_MAGIC && shared->Active != 0 && process_is_alive(shared->ServerPid);
        UnmapViewOfFile(shared);
    }
    CloseHandle(mapping);
    return available;
}

static DWORD lock_handle(M231_HANDLE *handle) {
    DWORD result;
    if (handle == NULL || handle->Mutex == NULL || handle->Shared == NULL) return FT_INVALID_HANDLE;
    result = WaitForSingleObject(handle->Mutex, 5000);
    return (result == WAIT_OBJECT_0 || result == WAIT_ABANDONED) ? FT_OK : FT_DEVICE_NOT_OPENED;
}

static void unlock_handle(M231_HANDLE *handle) {
    ReleaseMutex(handle->Mutex);
}

static DWORD validate_handle(void *raw, M231_HANDLE **output) {
    M231_HANDLE *handle = (M231_HANDLE *)raw;
    if (handle == NULL || handle->Shared == NULL || handle->Shared->Magic != M231_MAGIC) return FT_INVALID_HANDLE;
    *output = handle;
    return FT_OK;
}

__declspec(dllexport) DWORD WINAPI FT_CreateDeviceInfoList(DWORD *count) {
    if (count == NULL) return FT_INVALID_PARAMETER;
    *count = mapping_available() ? 1 : 0;
    return FT_OK;
}

__declspec(dllexport) DWORD WINAPI FT_GetDeviceInfoDetail(
    DWORD index, DWORD *flags, DWORD *type, DWORD *id, DWORD *location,
    char *serial, char *description, void **ftHandle) {
    if (index != 0 || !mapping_available()) return FT_DEVICE_NOT_FOUND;
    if (flags) *flags = 0;
    if (type) *type = FT_DEVICE_232R;
    if (id) *id = 0x04036001;
    if (location) *location = 0;
    if (serial) strcpy_s(serial, 16, SERIAL);
    if (description) strcpy_s(description, 64, DESCRIPTION);
    if (ftHandle) *ftHandle = NULL;
    return FT_OK;
}

__declspec(dllexport) DWORD WINAPI FT_ListDevices(void *argument1, void *argument2, DWORD flags) {
    if ((flags & 0x80000000u) != 0) {
        if (argument1 == NULL) return FT_INVALID_PARAMETER;
        *(DWORD *)argument1 = mapping_available() ? 1 : 0;
        return FT_OK;
    }
    if (!mapping_available()) return FT_DEVICE_NOT_FOUND;
    if (argument2 == NULL) return FT_INVALID_PARAMETER;
    if ((flags & 3u) == FT_OPEN_BY_DESCRIPTION) strcpy_s((char *)argument2, 64, DESCRIPTION);
    else strcpy_s((char *)argument2, 16, SERIAL);
    return FT_OK;
}

__declspec(dllexport) DWORD WINAPI FT_Open(int index, void **output) {
    HANDLE mapping;
    HANDLE mutex;
    M231_SHARED *shared;
    M231_HANDLE *handle;
    DWORD lockResult;
    DWORD curPid = GetCurrentProcessId();

    shim_log("FT_Open chamado: index=%d, output=%p", index, output);

    if (index != 0 || output == NULL) {
        shim_log("FT_Open -> FT_DEVICE_NOT_FOUND (index ou output invalido)");
        return FT_DEVICE_NOT_FOUND;
    }

    mapping = OpenFileMappingW(FILE_MAP_ALL_ACCESS, FALSE, MAPPING_NAME);
    if (mapping == NULL) {
        shim_log("FT_Open -> FT_DEVICE_NOT_FOUND (OpenFileMappingW falhou, err=%lu)",
                 (unsigned long)GetLastError());
        return FT_DEVICE_NOT_FOUND;
    }

    mutex = CreateMutexW(NULL, FALSE, MUTEX_NAME);
    if (mutex == NULL) {
        DWORD err = GetLastError();
        shim_log("FT_Open -> FT_DEVICE_NOT_OPENED (CreateMutexW falhou, err=%lu)",
                 (unsigned long)err);
        CloseHandle(mapping);
        return FT_DEVICE_NOT_OPENED;
    }

    shared = (M231_SHARED *)MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0, 0);
    if (shared == NULL) {
        DWORD err = GetLastError();
        shim_log("FT_Open -> FT_DEVICE_NOT_OPENED (MapViewOfFile falhou, err=%lu)",
                 (unsigned long)err);
        CloseHandle(mutex);
        CloseHandle(mapping);
        return FT_DEVICE_NOT_OPENED;
    }

    handle = (M231_HANDLE *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(*handle));
    if (handle == NULL) {
        DWORD err = GetLastError();
        shim_log("FT_Open -> FT_DEVICE_NOT_OPENED (HeapAlloc falhou, err=%lu)",
                 (unsigned long)err);
        UnmapViewOfFile(shared);
        CloseHandle(mutex);
        CloseHandle(mapping);
        return FT_DEVICE_NOT_OPENED;
    }

    handle->Mapping = mapping;
    handle->Mutex = mutex;
    handle->Shared = shared;
    handle->ReadTimeout = 100;
    handle->LatencyTimer = 2;

    lockResult = lock_handle(handle);
    if (lockResult != FT_OK) {
        shim_log("FT_Open -> status=%lu (lock_handle falhou)", (unsigned long)lockResult);
        UnmapViewOfFile(shared);
        CloseHandle(mutex);
        CloseHandle(mapping);
        HeapFree(GetProcessHeap(), 0, handle);
        return lockResult;
    }

    shim_log("Shared: Magic=0x%08lX Ver=%lu ServerPid=%lu ClientPid=%lu Active=%lu",
             (unsigned long)shared->Magic, (unsigned long)shared->Version,
             (unsigned long)shared->ServerPid, (unsigned long)shared->ClientPid,
             (unsigned long)shared->Active);

    if (shared->Magic != M231_MAGIC || !shared->Active || !process_is_alive(shared->ServerPid)) {
        shim_log("FT_Open -> FT_DEVICE_NOT_FOUND (Magic/Active/ServerPid invalido, srvAlive=%d)",
                 (int)process_is_alive(shared->ServerPid));
        unlock_handle(handle);
        UnmapViewOfFile(shared);
        CloseHandle(mutex);
        CloseHandle(mapping);
        HeapFree(GetProcessHeap(), 0, handle);
        return FT_DEVICE_NOT_FOUND;
    }

    if (shared->ClientPid != 0 && shared->ClientPid != curPid) {
        if (!process_is_alive(shared->ClientPid)) {
            shim_log("Limpando ClientPid morto: %lu", (unsigned long)shared->ClientPid);
            shared->ClientPid = 0;
        } else {
            shim_log("FT_Open -> FT_DEVICE_NOT_OPENED (ClientPid %lu ainda ativo)",
                     (unsigned long)shared->ClientPid);
            unlock_handle(handle);
            UnmapViewOfFile(shared);
            CloseHandle(mutex);
            CloseHandle(mapping);
            HeapFree(GetProcessHeap(), 0, handle);
            return FT_DEVICE_NOT_OPENED;
        }
    }

    shared->ClientPid = curPid;
    unlock_handle(handle);

    *output = handle;
    shim_log("FT_Open -> FT_OK (handle=%p, ClientPid=%lu)", handle, (unsigned long)curPid);
    return FT_OK;
}

__declspec(dllexport) DWORD WINAPI FT_OpenEx(void *argument, DWORD flags, void **output) {
    const char *text = (const char *)argument;
    if (text == NULL) return FT_INVALID_PARAMETER;
    if ((flags & 3u) == FT_OPEN_BY_SERIAL_NUMBER && strcmp(text, SERIAL) != 0) return FT_DEVICE_NOT_FOUND;
    if ((flags & 3u) == FT_OPEN_BY_DESCRIPTION && strcmp(text, DESCRIPTION) != 0) return FT_DEVICE_NOT_FOUND;
    return FT_Open(0, output);
}

/*
 * API compatível usada pelo transporte da controladora. Ela usa handles inteiros e nomes
 * distintos dos aliases D2XX atuais. Os wrappers mantêm um unico canal
 * interno, mas deixam a chamada observavel no log para a captura do
 * protocolo legado.
 */
__declspec(dllexport) DWORD WINAPI FT_GetNumberOfDevices(int *count, char **names, int flags) {
    DWORD available = mapping_available() ? 1 : 0;
    if (count == NULL) return FT_INVALID_PARAMETER;
    *count = (int)available;
    if (names != NULL && available != 0 && *names != NULL) {
        strcpy_s(*names, 16, SERIAL);
    }
    shim_log("FT_GetNumberOfDevices -> count=%d flags=%d", *count, flags);
    return FT_OK;
}

__declspec(dllexport) DWORD WINAPI FT_GetDeviceString(int index, char **value, int flags) {
    if (value == NULL || *value == NULL || index != 0 || !mapping_available()) {
        return FT_DEVICE_NOT_FOUND;
    }
    if ((flags & 3) == FT_OPEN_BY_DESCRIPTION) strcpy_s(*value, 64, DESCRIPTION);
    else strcpy_s(*value, 16, SERIAL);
    shim_log("FT_GetDeviceString -> index=%d flags=%d", index, flags);
    return FT_OK;
}

__declspec(dllexport) DWORD WINAPI FT_OpenByIndex(int index, int *output) {
    void *handle = NULL;
    DWORD status;
    if (output == NULL) return FT_INVALID_PARAMETER;
    status = FT_Open(index, &handle);
    *output = (int)(intptr_t)handle;
    shim_log("FT_OpenByIndex -> status=%lu handle=%p", (unsigned long)status, handle);
    return status;
}

__declspec(dllexport) DWORD WINAPI FT_OpenBySerialNumber(char **serial, int flags, int *output) {
    void *handle = NULL;
    DWORD status;
    if (serial == NULL || *serial == NULL || output == NULL) return FT_INVALID_PARAMETER;
    if (strcmp(*serial, SERIAL) != 0) return FT_DEVICE_NOT_FOUND;
    status = FT_Open(0, &handle);
    *output = (int)(intptr_t)handle;
    shim_log("FT_OpenBySerialNumber -> status=%lu flags=%d handle=%p", (unsigned long)status, flags, handle);
    return status;
}

__declspec(dllexport) DWORD WINAPI FT_OpenByDescription(char **description, int flags, int *output) {
    void *handle = NULL;
    DWORD status;
    if (description == NULL || *description == NULL || output == NULL) return FT_INVALID_PARAMETER;
    if (strcmp(*description, DESCRIPTION) != 0) return FT_DEVICE_NOT_FOUND;
    status = FT_Open(0, &handle);
    *output = (int)(intptr_t)handle;
    shim_log("FT_OpenByDescription -> status=%lu flags=%d handle=%p", (unsigned long)status, flags, handle);
    return status;
}

__declspec(dllexport) DWORD WINAPI FT_Close(void *raw) {
    M231_HANDLE *handle;
    DWORD curPid = GetCurrentProcessId();

    if (validate_handle(raw, &handle) != FT_OK) {
        shim_log("FT_Close -> FT_INVALID_HANDLE (raw=%p)", raw);
        return FT_INVALID_HANDLE;
    }

    if (lock_handle(handle) == FT_OK) {
        if (handle->Shared->ClientPid == curPid) {
            handle->Shared->ClientPid = 0;
        }
        unlock_handle(handle);
    }

    UnmapViewOfFile(handle->Shared);
    CloseHandle(handle->Mutex);
    CloseHandle(handle->Mapping);
    handle->Shared = NULL;
    HeapFree(GetProcessHeap(), 0, handle);

    shim_log("FT_Close -> FT_OK (handle=%p)", raw);
    return FT_OK;
}

__declspec(dllexport) DWORD WINAPI FT_Read(void *raw, void *buffer, DWORD requested, DWORD *readCount) {
    M231_HANDLE *handle;
    DWORD started = GetTickCount();
    DWORD status = validate_handle(raw, &handle);
    if (readCount) *readCount = 0;
    if (status != FT_OK || buffer == NULL || readCount == NULL) return status != FT_OK ? status : FT_INVALID_PARAMETER;
    for (;;) {
        status = lock_handle(handle);
        if (status != FT_OK) return status;
        if (handle->Shared->RxLength != 0) {
            DWORD count = min(requested, handle->Shared->RxLength);
            BYTE *rx = (BYTE *)handle->Shared + M231_RX_OFFSET;
            memcpy(buffer, rx, count);
            shim_log_bytes("FT_Read bytes", buffer, count);
            memmove(rx, rx + count, handle->Shared->RxLength - count);
            handle->Shared->RxLength -= count;
            handle->Shared->ReadCount += 1;
            *readCount = count;
            unlock_handle(handle);
            return FT_OK;
        }
        unlock_handle(handle);
        if (handle->ReadTimeout == 0 || GetTickCount() - started >= handle->ReadTimeout) return FT_OK;
        Sleep(1);
    }
}

__declspec(dllexport) DWORD WINAPI FT_Write(void *raw, const void *buffer, DWORD requested, DWORD *written) {
    M231_HANDLE *handle;
    DWORD status = validate_handle(raw, &handle);
    DWORD count;
    BYTE *tx;
    if (written) *written = 0;
    if (status != FT_OK || buffer == NULL || written == NULL) return status != FT_OK ? status : FT_INVALID_PARAMETER;
    status = lock_handle(handle);
    if (status != FT_OK) return status;
    count = min(requested, M231_CAPACITY - handle->Shared->TxLength);
    tx = (BYTE *)handle->Shared + M231_TX_OFFSET + handle->Shared->TxLength;
    memcpy(tx, buffer, count);
    shim_log_bytes("FT_Write bytes", (const BYTE *)buffer, count);
    handle->Shared->TxLength += count;
    handle->Shared->WriteCount += 1;
    *written = count;
    unlock_handle(handle);
    return count == requested ? FT_OK : FT_DEVICE_NOT_OPENED;
}

__declspec(dllexport) DWORD WINAPI FT_Read_Bytes(int raw, BYTE *buffer, int requested, int *readCount) {
    DWORD count = 0;
    DWORD status = FT_Read((void *)(intptr_t)raw, buffer, (DWORD)requested, &count);
    if (readCount != NULL) *readCount = (int)count;
    shim_log("FT_Read_Bytes -> status=%lu requested=%d read=%lu", (unsigned long)status, requested, (unsigned long)count);
    return status;
}

__declspec(dllexport) DWORD WINAPI FT_Write_Bytes(int raw, BYTE *buffer, int requested, int *written) {
    DWORD count = 0;
    DWORD status = FT_Write((void *)(intptr_t)raw, buffer, (DWORD)requested, &count);
    if (written != NULL) *written = (int)count;
    shim_log("FT_Write_Bytes -> status=%lu requested=%d written=%lu", (unsigned long)status, requested, (unsigned long)count);
    return status;
}

/* Nomes usados pela camada P/Invoke do transporte compatível. */
__declspec(dllexport) DWORD WINAPI FT_Read_String(int raw, char *buffer, int requested, int *readCount) {
    DWORD status = FT_Read_Bytes(raw, (BYTE *)buffer, requested, readCount);
    shim_log("FT_Read_String -> status=%lu requested=%d read=%d", (unsigned long)status,
             requested, readCount != NULL ? *readCount : 0);
    return status;
}

__declspec(dllexport) DWORD WINAPI FT_Write_String(int raw, char *buffer, int requested, int *written) {
    DWORD status = FT_Write_Bytes(raw, (BYTE *)buffer, requested, written);
    shim_log("FT_Write_String -> status=%lu requested=%d written=%d", (unsigned long)status,
             requested, written != NULL ? *written : 0);
    return status;
}

__declspec(dllexport) DWORD WINAPI FT_GetQueueStatus(void *raw, DWORD *count) {
    M231_HANDLE *handle; DWORD status = validate_handle(raw, &handle);
    if (status != FT_OK || count == NULL) return status != FT_OK ? status : FT_INVALID_PARAMETER;
    status = lock_handle(handle); if (status != FT_OK) return status;
    *count = handle->Shared->RxLength; unlock_handle(handle); return FT_OK;
}

__declspec(dllexport) DWORD WINAPI FT_GetStatus(void *raw, DWORD *rx, DWORD *tx, DWORD *events) {
    M231_HANDLE *handle; DWORD status = validate_handle(raw, &handle);
    if (status != FT_OK) return status;
    status = lock_handle(handle); if (status != FT_OK) return status;
    if (rx) *rx = handle->Shared->RxLength; if (tx) *tx = 0; if (events) *events = 0;
    unlock_handle(handle); return FT_OK;
}

__declspec(dllexport) DWORD WINAPI FT_Purge(void *raw, DWORD mask) {
    M231_HANDLE *handle; DWORD status = validate_handle(raw, &handle); BYTE *base;
    if (status != FT_OK) return status;
    status = lock_handle(handle); if (status != FT_OK) return status;
    base = (BYTE *)handle->Shared;
    if (mask & FT_PURGE_RX) { handle->Shared->RxLength = 0; ZeroMemory(base + M231_RX_OFFSET, M231_CAPACITY); }
    if (mask & FT_PURGE_TX) { handle->Shared->TxLength = 0; ZeroMemory(base + M231_TX_OFFSET, M231_CAPACITY); }
    unlock_handle(handle); return FT_OK;
}

__declspec(dllexport) DWORD WINAPI FT_SetTimeouts(void *raw, DWORD readTimeout, DWORD writeTimeout) {
    M231_HANDLE *handle; (void)writeTimeout;
    if (validate_handle(raw, &handle) != FT_OK) return FT_INVALID_HANDLE;
    handle->ReadTimeout = readTimeout; return FT_OK;
}

__declspec(dllexport) DWORD WINAPI FT_SetLatencyTimer(void *raw, BYTE value) {
    M231_HANDLE *handle; if (validate_handle(raw, &handle) != FT_OK) return FT_INVALID_HANDLE;
    handle->LatencyTimer = value; return FT_OK;
}

__declspec(dllexport) DWORD WINAPI FT_GetLatencyTimer(void *raw, BYTE *value) {
    M231_HANDLE *handle; if (validate_handle(raw, &handle) != FT_OK) return FT_INVALID_HANDLE;
    if (value == NULL) return FT_INVALID_PARAMETER; *value = handle->LatencyTimer; return FT_OK;
}

__declspec(dllexport) DWORD WINAPI FT_GetDeviceInfo(void *raw, DWORD *type, DWORD *id, char *serial, char *description, void *dummy) {
    M231_HANDLE *handle; (void)dummy; if (validate_handle(raw, &handle) != FT_OK) return FT_INVALID_HANDLE;
    if (type) *type = FT_DEVICE_232R; if (id) *id = 0x04036001;
    if (serial) strcpy_s(serial, 16, SERIAL); if (description) strcpy_s(description, 64, DESCRIPTION); return FT_OK;
}

#define SIMPLE1(name) __declspec(dllexport) DWORD WINAPI name(void *h) { M231_HANDLE *x; return validate_handle(h, &x); }
#define SIMPLE2(name, type2) __declspec(dllexport) DWORD WINAPI name(void *h, type2 a) { M231_HANDLE *x; (void)a; return validate_handle(h, &x); }
#define SIMPLE3(name, type2, type3) __declspec(dllexport) DWORD WINAPI name(void *h, type2 a, type3 b) { M231_HANDLE *x; (void)a; (void)b; return validate_handle(h, &x); }
#define SIMPLE4(name, type2, type3, type4) __declspec(dllexport) DWORD WINAPI name(void *h, type2 a, type3 b, type4 c) { M231_HANDLE *x; (void)a; (void)b; (void)c; return validate_handle(h, &x); }

SIMPLE1(FT_ResetDevice)
SIMPLE1(FT_CyclePort)
SIMPLE1(FT_SetBreakOn)
SIMPLE1(FT_SetBreakOff)
SIMPLE1(FT_SetDtr)
SIMPLE1(FT_ClrDtr)
SIMPLE1(FT_SetRts)
SIMPLE1(FT_ClrRts)
SIMPLE1(FT_EraseEE)
SIMPLE2(FT_SetBaudRate, DWORD)
SIMPLE2(FT_SetDivisor, DWORD)
SIMPLE2(FT_SetDeadmanTimeout, DWORD)
SIMPLE2(FT_SetUSBParameters, DWORD)
SIMPLE2(FT_SetEventNotification, DWORD)
SIMPLE3(FT_SetBitMode, BYTE, BYTE)
SIMPLE3(FT_SetFlowControl, USHORT, BYTE)
SIMPLE4(FT_SetDataCharacteristics, BYTE, BYTE, BYTE)
SIMPLE4(FT_SetChars, BYTE, BYTE, BYTE)

__declspec(dllexport) DWORD WINAPI FT_GetBitMode(void *raw, BYTE *value) { M231_HANDLE *h; if (validate_handle(raw, &h) != FT_OK) return FT_INVALID_HANDLE; if (!value) return FT_INVALID_PARAMETER; *value = 0; return FT_OK; }
__declspec(dllexport) DWORD WINAPI FT_GetModemStatus(void *raw, DWORD *value) { M231_HANDLE *h; if (validate_handle(raw, &h) != FT_OK) return FT_INVALID_HANDLE; if (!value) return FT_INVALID_PARAMETER; *value = 0x30; return FT_OK; }
__declspec(dllexport) DWORD WINAPI FT_EE_UASize(void *raw, DWORD *value) { M231_HANDLE *h; if (validate_handle(raw, &h) != FT_OK) return FT_INVALID_HANDLE; if (!value) return FT_INVALID_PARAMETER; *value = 0; return FT_OK; }
__declspec(dllexport) DWORD WINAPI FT_EE_UARead(void *raw, BYTE *data, DWORD size, DWORD *readCount) { M231_HANDLE *h; (void)data; (void)size; if (validate_handle(raw, &h) != FT_OK) return FT_INVALID_HANDLE; if (readCount) *readCount = 0; return FT_OK; }
__declspec(dllexport) DWORD WINAPI FT_EE_UAWrite(void *raw, BYTE *data, DWORD size) { M231_HANDLE *h; (void)data; (void)size; return validate_handle(raw, &h); }
__declspec(dllexport) DWORD WINAPI FT_EE_ReadEx(void *raw, void *data, char *m, char *mi, char *d, char *s) { M231_HANDLE *h; (void)data; if (validate_handle(raw, &h) != FT_OK) return FT_INVALID_HANDLE; if(m) strcpy_s(m, 32, "FTDI"); if(mi) strcpy_s(mi, 16, "FT232R"); if(d) strcpy_s(d, 64, DESCRIPTION); if(s) strcpy_s(s, 16, SERIAL); return FT_OK; }
__declspec(dllexport) DWORD WINAPI FT_EE_ProgramEx(void *raw, void *data, char *m, char *mi, char *d, char *s) { M231_HANDLE *h; (void)data; (void)m; (void)mi; (void)d; (void)s; return validate_handle(raw, &h); }

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID reserved) {
    (void)instance; (void)reason; (void)reserved; return TRUE;
}
