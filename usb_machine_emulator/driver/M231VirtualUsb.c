#include "M231VirtualUsb.h"

static const UCHAR M231_DEVICE_DESCRIPTOR[] = {
    0x12, USB_DEVICE_DESCRIPTOR_TYPE,
    0x00, 0x02,
    0x00, 0x00, 0x00,
    0x08,
    0x03, 0x04,
    0x01, 0x60,
    0x00, 0x06,
    0x01, 0x02, 0x03,
    0x01
};

static const UCHAR M231_CONFIGURATION_DESCRIPTOR[] = {
    0x09, USB_CONFIGURATION_DESCRIPTOR_TYPE,
    0x20, 0x00,
    0x01, 0x01, 0x00,
    0x80, 0x32,

    0x09, USB_INTERFACE_DESCRIPTOR_TYPE,
    0x00, 0x00, 0x02,
    0xFF, 0xFF, 0xFF,
    0x02,

    0x07, USB_ENDPOINT_DESCRIPTOR_TYPE,
    0x81, USB_ENDPOINT_TYPE_BULK,
    0x40, 0x00, 0x00,

    0x07, USB_ENDPOINT_DESCRIPTOR_TYPE,
    0x02, USB_ENDPOINT_TYPE_BULK,
    0x40, 0x00, 0x00
};

/*
 * Factory-compatible FT232R EEPROM image (64 16-bit words).  FTDIBUS reads
 * this image with vendor request 0x90 while starting the device.  Returning
 * zeroes makes the Windows driver reject the emulated FT232R with
 * STATUS_NOT_SUPPORTED.  The final word contains the FTDI checksum.
 */
static const UCHAR M231_FT232R_EEPROM[] = {
    0x00, 0x40, 0x03, 0x04, 0x01, 0x60, 0x00, 0x00,
    0xA0, 0x2D, 0x08, 0x00, 0x00, 0x00, 0x98, 0x0A,
    0xA2, 0x20, 0xC2, 0x12, 0x23, 0x10, 0x05, 0x00,
    0x0A, 0x03, 0x46, 0x00, 0x54, 0x00, 0x44, 0x00,
    0x49, 0x00, 0x20, 0x03, 0x46, 0x00, 0x54, 0x00,
    0x32, 0x00, 0x33, 0x00, 0x32, 0x00, 0x52, 0x00,
    0x20, 0x00, 0x55, 0x00, 0x53, 0x00, 0x42, 0x00,
    0x20, 0x00, 0x55, 0x00, 0x41, 0x00, 0x52, 0x00,
    0x54, 0x00, 0x12, 0x03, 0x41, 0x00, 0x35, 0x00,
    0x30, 0x00, 0x31, 0x00, 0x47, 0x00, 0x52, 0x00,
    0x36, 0x00, 0x59, 0x00, 0x06, 0x41, 0x3F, 0x50,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xCD, 0xFA
};

static VOID
M231BufferAppend(
    _Inout_updates_(Capacity) PUCHAR Buffer,
    _Inout_ PULONG Length,
    _In_ ULONG Capacity,
    _In_reads_bytes_(DataLength) const UCHAR *Data,
    _In_ ULONG DataLength
    )
{
    ULONG keep;

    if (DataLength >= Capacity) {
        RtlCopyMemory(Buffer, Data + DataLength - Capacity, Capacity);
        *Length = Capacity;
        return;
    }
    if (*Length + DataLength > Capacity) {
        keep = Capacity - DataLength;
        RtlMoveMemory(Buffer, Buffer + (*Length - keep), keep);
        *Length = keep;
    }
    RtlCopyMemory(Buffer + *Length, Data, DataLength);
    *Length += DataLength;
}

static ULONG
M231BufferTake(
    _Inout_updates_(Capacity) PUCHAR Buffer,
    _Inout_ PULONG Length,
    _In_ ULONG Capacity,
    _Out_writes_bytes_(Requested) PUCHAR Output,
    _In_ ULONG Requested
    )
{
    ULONG count = min(*Length, min(Capacity, Requested));
    if (count != 0) {
        RtlCopyMemory(Output, Buffer, count);
        RtlMoveMemory(Buffer, Buffer + count, *Length - count);
        *Length -= count;
    }
    return count;
}

static NTSTATUS
M231CreateEndpoint(
    _In_ WDFDEVICE Device,
    _In_ UCHAR Address,
    _In_ WDF_IO_QUEUE_DISPATCH_TYPE DispatchType,
    _In_opt_ EVT_WDF_IO_QUEUE_IO_INTERNAL_DEVICE_CONTROL *Callback,
    _Out_ WDFQUEUE *Queue,
    _Out_ UDECXUSBENDPOINT *Endpoint
    )
{
    PDEVICE_CONTEXT context = DeviceGetContext(Device);
    WDF_IO_QUEUE_CONFIG queueConfig;
    WDF_OBJECT_ATTRIBUTES queueAttributes;
    UDECX_USB_ENDPOINT_CALLBACKS endpointCallbacks;
    PUDECXUSBENDPOINT_INIT endpointInit = NULL;
    NTSTATUS status;

    WDF_IO_QUEUE_CONFIG_INIT(&queueConfig, DispatchType);
    if (Callback != NULL) {
        queueConfig.EvtIoInternalDeviceControl = Callback;
    }
    WDF_OBJECT_ATTRIBUTES_INIT(&queueAttributes);
    queueAttributes.ParentObject = context->UsbDevice;
    status = WdfIoQueueCreate(Device, &queueConfig, &queueAttributes, Queue);
    if (!NT_SUCCESS(status)) {
        return status;
    }

    endpointInit = UdecxUsbSimpleEndpointInitAllocate(context->UsbDevice);
    if (endpointInit == NULL) {
        return STATUS_INSUFFICIENT_RESOURCES;
    }
    UdecxUsbEndpointInitSetEndpointAddress(endpointInit, Address);
    UDECX_USB_ENDPOINT_CALLBACKS_INIT(&endpointCallbacks, M231EvtEndpointReset);
    UdecxUsbEndpointInitSetCallbacks(endpointInit, &endpointCallbacks);
    status = UdecxUsbEndpointCreate(&endpointInit, WDF_NO_OBJECT_ATTRIBUTES, Endpoint);
    if (!NT_SUCCESS(status)) {
        if (endpointInit != NULL) {
            UdecxUsbEndpointInitFree(endpointInit);
        }
        return status;
    }
    UdecxUsbEndpointSetWdfIoQueue(*Endpoint, *Queue);
    return STATUS_SUCCESS;
}

NTSTATUS
M231CreateAndPlugIn(
    _In_ WDFDEVICE Device
    )
{
    PDEVICE_CONTEXT context = DeviceGetContext(Device);
    PUDECXUSBDEVICE_INIT deviceInit = NULL;
    UDECX_USB_DEVICE_PLUG_IN_OPTIONS plugOptions;
    UNICODE_STRING manufacturer;
    UNICODE_STRING product;
    UNICODE_STRING serial;
    WDFQUEUE controlQueue = NULL;
    WDFQUEUE bulkOutQueue = NULL;
    NTSTATUS status;

    if (context->PluggedIn) {
        return STATUS_SUCCESS;
    }

    deviceInit = UdecxUsbDeviceInitAllocate(Device);
    if (deviceInit == NULL) {
        return STATUS_INSUFFICIENT_RESOURCES;
    }
    UdecxUsbDeviceInitSetSpeed(deviceInit, UdecxUsbFullSpeed);
    UdecxUsbDeviceInitSetEndpointsType(deviceInit, UdecxEndpointTypeSimple);

    status = UdecxUsbDeviceInitAddDescriptor(
        deviceInit,
        (PUCHAR)M231_DEVICE_DESCRIPTOR,
        sizeof(M231_DEVICE_DESCRIPTOR));
    if (!NT_SUCCESS(status)) {
        goto Exit;
    }
    status = UdecxUsbDeviceInitAddDescriptor(
        deviceInit,
        (PUCHAR)M231_CONFIGURATION_DESCRIPTOR,
        sizeof(M231_CONFIGURATION_DESCRIPTOR));
    if (!NT_SUCCESS(status)) {
        goto Exit;
    }

    RtlInitUnicodeString(&manufacturer, L"FTDI");
    /* Identidade da FT232R da controladora original, tambem aceita pelo
     * M231Pro novo que abre o primeiro dispositivo D2XX disponivel. */
    RtlInitUnicodeString(&product, L"FT232R USB UART");
    RtlInitUnicodeString(&serial, L"A501GR6Y");
    status = UdecxUsbDeviceInitAddStringDescriptor(deviceInit, &manufacturer, 1, 0x0409);
    if (!NT_SUCCESS(status)) {
        goto Exit;
    }
    status = UdecxUsbDeviceInitAddStringDescriptor(deviceInit, &product, 2, 0x0409);
    if (!NT_SUCCESS(status)) {
        goto Exit;
    }
    status = UdecxUsbDeviceInitAddStringDescriptor(deviceInit, &serial, 3, 0x0409);
    if (!NT_SUCCESS(status)) {
        goto Exit;
    }

    status = UdecxUsbDeviceCreate(&deviceInit, WDF_NO_OBJECT_ATTRIBUTES, &context->UsbDevice);
    if (!NT_SUCCESS(status)) {
        goto Exit;
    }

    status = M231CreateEndpoint(
        Device,
        USB_DEFAULT_ENDPOINT_ADDRESS,
        WdfIoQueueDispatchSequential,
        M231EvtControlUrb,
        &controlQueue,
        &context->ControlEndpoint);
    if (!NT_SUCCESS(status)) {
        goto Exit;
    }
    status = M231CreateEndpoint(
        Device,
        0x81,
        WdfIoQueueDispatchManual,
        NULL,
        &context->BulkInQueue,
        &context->BulkInEndpoint);
    if (!NT_SUCCESS(status)) {
        goto Exit;
    }
    status = M231CreateEndpoint(
        Device,
        0x02,
        WdfIoQueueDispatchSequential,
        M231EvtBulkOutUrb,
        &bulkOutQueue,
        &context->BulkOutEndpoint);
    if (!NT_SUCCESS(status)) {
        goto Exit;
    }

    UDECX_USB_DEVICE_PLUG_IN_OPTIONS_INIT(&plugOptions);
    plugOptions.Usb20PortNumber = 1;
    status = UdecxUsbDevicePlugIn(context->UsbDevice, &plugOptions);
    if (NT_SUCCESS(status)) {
        context->PluggedIn = TRUE;
    }

Exit:
    if (deviceInit != NULL) {
        UdecxUsbDeviceInitFree(deviceInit);
    }
    if (!NT_SUCCESS(status) && context->UsbDevice != NULL) {
        UdecxUsbDevicePlugOutAndDelete(context->UsbDevice);
        context->UsbDevice = NULL;
        context->ControlEndpoint = NULL;
        context->BulkInEndpoint = NULL;
        context->BulkOutEndpoint = NULL;
        context->BulkInQueue = NULL;
    }
    return status;
}

NTSTATUS
M231PlugOut(
    _In_ WDFDEVICE Device
    )
{
    PDEVICE_CONTEXT context = DeviceGetContext(Device);
    NTSTATUS status = STATUS_SUCCESS;

    if (context->UsbDevice != NULL) {
        status = UdecxUsbDevicePlugOutAndDelete(context->UsbDevice);
    }
    context->UsbDevice = NULL;
    context->ControlEndpoint = NULL;
    context->BulkInEndpoint = NULL;
    context->BulkOutEndpoint = NULL;
    context->BulkInQueue = NULL;
    context->PluggedIn = FALSE;
    context->RxLength = 0;
    context->TxLength = 0;
    InterlockedExchange(&context->HostReadCount, 0);
    InterlockedExchange(&context->HostWriteCount, 0);
    return status;
}

static VOID
M231CompletePendingBulkIn(
    _In_ WDFDEVICE Device
    )
{
    PDEVICE_CONTEXT context = DeviceGetContext(Device);
    WDFREQUEST request;
    PUCHAR transferBuffer;
    ULONG transferLength;
    ULONG copied;
    NTSTATUS status;

    if (context->BulkInQueue == NULL || context->RxLength == 0) {
        return;
    }
    status = WdfIoQueueRetrieveNextRequest(context->BulkInQueue, &request);
    if (!NT_SUCCESS(status)) {
        return;
    }
    status = UdecxUrbRetrieveBuffer(request, &transferBuffer, &transferLength);
    if (!NT_SUCCESS(status) || transferLength < 2) {
        UdecxUrbCompleteWithNtStatus(request, NT_SUCCESS(status) ? STATUS_BUFFER_TOO_SMALL : status);
        return;
    }

    transferBuffer[0] = 0x10;
    transferBuffer[1] = 0x00;
    WdfSpinLockAcquire(context->BufferLock);
    copied = M231BufferTake(
        context->RxBuffer,
        &context->RxLength,
        M231_BUFFER_CAPACITY,
        transferBuffer + 2,
        transferLength - 2);
    WdfSpinLockRelease(context->BufferLock);
    UdecxUrbSetBytesCompleted(request, copied + 2);
    UdecxUrbCompleteWithNtStatus(request, STATUS_SUCCESS);
    InterlockedIncrement(&context->HostReadCount);
}

VOID
M231EvtControlUrb(
    _In_ WDFQUEUE Queue,
    _In_ WDFREQUEST Request,
    _In_ size_t OutputBufferLength,
    _In_ size_t InputBufferLength,
    _In_ ULONG IoControlCode
    )
{
    WDF_USB_CONTROL_SETUP_PACKET setupPacket;
    PUCHAR transferBuffer = NULL;
    ULONG transferLength = 0;
    ULONG completed = 0;
    NTSTATUS status;

    UNREFERENCED_PARAMETER(Queue);
    UNREFERENCED_PARAMETER(OutputBufferLength);
    UNREFERENCED_PARAMETER(InputBufferLength);

    if (IoControlCode != IOCTL_INTERNAL_USB_SUBMIT_URB) {
        UdecxUrbCompleteWithNtStatus(Request, STATUS_INVALID_DEVICE_REQUEST);
        return;
    }
    status = UdecxUrbRetrieveControlSetupPacket(Request, &setupPacket);
    if (!NT_SUCCESS(status)) {
        UdecxUrbCompleteWithNtStatus(Request, status);
        return;
    }
    status = UdecxUrbRetrieveBuffer(Request, &transferBuffer, &transferLength);
    if (!NT_SUCCESS(status)) {
        transferBuffer = NULL;
        transferLength = 0;
        status = STATUS_SUCCESS;
    }

    if (setupPacket.Packet.bm.Request.Type == BmRequestStandard) {
        status = STATUS_SUCCESS;
        if (setupPacket.Packet.bm.Request.Dir == BmRequestDeviceToHost && transferBuffer != NULL) {
            switch (setupPacket.Packet.bRequest) {
            case USB_REQUEST_GET_STATUS:
                completed = min(transferLength, min((ULONG)setupPacket.Packet.wLength, 2UL));
                RtlZeroMemory(transferBuffer, completed);
                break;

            case USB_REQUEST_GET_CONFIGURATION:
                completed = min(transferLength, min((ULONG)setupPacket.Packet.wLength, 1UL));
                if (completed != 0) {
                    transferBuffer[0] = 1;
                }
                break;

            case USB_REQUEST_GET_INTERFACE:
                completed = min(transferLength, min((ULONG)setupPacket.Packet.wLength, 1UL));
                if (completed != 0) {
                    transferBuffer[0] = 0;
                }
                break;

            default:
                completed = 0;
                break;
            }
        }
    } else if (setupPacket.Packet.bm.Request.Type == BmRequestVendor &&
               setupPacket.Packet.bm.Request.Dir == BmRequestDeviceToHost &&
               transferBuffer != NULL) {
        completed = min(transferLength, setupPacket.Packet.wLength);
        RtlZeroMemory(transferBuffer, completed);
        if (setupPacket.Packet.bRequest == 0x05 && completed >= 2) {
            transferBuffer[0] = 0x10;
            transferBuffer[1] = 0x00;
        } else if (setupPacket.Packet.bRequest == 0x0A && completed >= 1) {
            transferBuffer[0] = 1;
        } else if (setupPacket.Packet.bRequest == 0x90 && completed >= 2) {
            ULONG offset = (ULONG)setupPacket.Packet.wIndex.Value * 2;
            if (offset + 1 < sizeof(M231_FT232R_EEPROM)) {
                transferBuffer[0] = M231_FT232R_EEPROM[offset];
                transferBuffer[1] = M231_FT232R_EEPROM[offset + 1];
                completed = 2;
            } else {
                completed = 0;
            }
        }
    } else if (setupPacket.Packet.bm.Request.Type == BmRequestVendor) {
        status = STATUS_SUCCESS;
    } else {
        status = STATUS_INVALID_DEVICE_REQUEST;
    }

    UdecxUrbSetBytesCompleted(Request, completed);
    UdecxUrbCompleteWithNtStatus(Request, status);
}

VOID
M231EvtBulkOutUrb(
    _In_ WDFQUEUE Queue,
    _In_ WDFREQUEST Request,
    _In_ size_t OutputBufferLength,
    _In_ size_t InputBufferLength,
    _In_ ULONG IoControlCode
    )
{
    WDFDEVICE device = WdfIoQueueGetDevice(Queue);
    PDEVICE_CONTEXT context = DeviceGetContext(device);
    PUCHAR transferBuffer;
    ULONG transferLength;
    NTSTATUS status;

    UNREFERENCED_PARAMETER(OutputBufferLength);
    UNREFERENCED_PARAMETER(InputBufferLength);

    if (IoControlCode != IOCTL_INTERNAL_USB_SUBMIT_URB) {
        UdecxUrbCompleteWithNtStatus(Request, STATUS_INVALID_DEVICE_REQUEST);
        return;
    }
    status = UdecxUrbRetrieveBuffer(Request, &transferBuffer, &transferLength);
    if (NT_SUCCESS(status)) {
        WdfSpinLockAcquire(context->BufferLock);
        M231BufferAppend(
            context->TxBuffer,
            &context->TxLength,
            M231_BUFFER_CAPACITY,
            transferBuffer,
            transferLength);
        WdfSpinLockRelease(context->BufferLock);
        UdecxUrbSetBytesCompleted(Request, transferLength);
        InterlockedIncrement(&context->HostWriteCount);
    }
    UdecxUrbCompleteWithNtStatus(Request, status);
}

VOID
M231EvtEndpointReset(
    _In_ UDECXUSBENDPOINT UdecxUsbEndpoint,
    _In_ WDFREQUEST Request
    )
{
    UNREFERENCED_PARAMETER(UdecxUsbEndpoint);
    WdfRequestComplete(Request, STATUS_SUCCESS);
}

VOID
M231EvtIoDeviceControl(
    _In_ WDFQUEUE Queue,
    _In_ WDFREQUEST Request,
    _In_ size_t OutputBufferLength,
    _In_ size_t InputBufferLength,
    _In_ ULONG IoControlCode
    )
{
    WDFDEVICE device = WdfIoQueueGetDevice(Queue);
    PDEVICE_CONTEXT context = DeviceGetContext(device);
    PVOID buffer;
    size_t length;
    ULONG copied;
    NTSTATUS status = STATUS_SUCCESS;

    UNREFERENCED_PARAMETER(OutputBufferLength);
    UNREFERENCED_PARAMETER(InputBufferLength);

    switch (IoControlCode) {
    case IOCTL_M231_PLUG_IN:
        status = M231CreateAndPlugIn(device);
        break;

    case IOCTL_M231_PLUG_OUT:
        status = M231PlugOut(device);
        break;

    case IOCTL_M231_PUSH_RX:
        status = WdfRequestRetrieveInputBuffer(Request, 1, &buffer, &length);
        if (NT_SUCCESS(status)) {
            WdfSpinLockAcquire(context->BufferLock);
            M231BufferAppend(
                context->RxBuffer,
                &context->RxLength,
                M231_BUFFER_CAPACITY,
                (const UCHAR *)buffer,
                (ULONG)length);
            WdfSpinLockRelease(context->BufferLock);
            M231CompletePendingBulkIn(device);
        }
        break;

    case IOCTL_M231_PULL_TX:
        status = WdfRequestRetrieveOutputBuffer(Request, 1, &buffer, &length);
        if (NT_SUCCESS(status)) {
            WdfSpinLockAcquire(context->BufferLock);
            copied = M231BufferTake(
                context->TxBuffer,
                &context->TxLength,
                M231_BUFFER_CAPACITY,
                (PUCHAR)buffer,
                (ULONG)length);
            WdfSpinLockRelease(context->BufferLock);
            WdfRequestSetInformation(Request, copied);
        }
        break;

    case IOCTL_M231_GET_STATUS:
        status = WdfRequestRetrieveOutputBuffer(Request, sizeof(M231_DRIVER_STATUS), &buffer, &length);
        if (NT_SUCCESS(status)) {
            PM231_DRIVER_STATUS driverStatus = (PM231_DRIVER_STATUS)buffer;
            UNREFERENCED_PARAMETER(length);
            driverStatus->Version = 3;
            driverStatus->PluggedIn = context->PluggedIn ? 1 : 0;
            driverStatus->RxBytesQueued = context->RxLength;
            driverStatus->TxBytesQueued = context->TxLength;
            driverStatus->HostReadCount = (ULONG)InterlockedCompareExchange(
                &context->HostReadCount,
                0,
                0);
            driverStatus->HostWriteCount = (ULONG)InterlockedCompareExchange(
                &context->HostWriteCount,
                0,
                0);
            WdfRequestSetInformation(Request, sizeof(*driverStatus));
        }
        break;

    default:
        if (UdecxWdfDeviceTryHandleUserIoctl(device, Request)) {
            return;
        }
        status = STATUS_INVALID_DEVICE_REQUEST;
        break;
    }
    WdfRequestComplete(Request, status);
}

NTSTATUS
M231EvtQueryUsbCapability(
    _In_ WDFDEVICE UdecxWdfDevice,
    _In_ PGUID CapabilityType,
    _In_ ULONG OutputBufferLength,
    _Out_writes_to_opt_(OutputBufferLength, *ResultLength) PVOID OutputBuffer,
    _Out_ PULONG ResultLength
    )
{
    UNREFERENCED_PARAMETER(UdecxWdfDevice);
    *ResultLength = 0;

    /*
     * We retrieve transfer data as one contiguous buffer.  Reporting chained
     * MDL support allows UdeCx to pass a TransferBufferMdl chain that this
     * driver never walks, which can corrupt a transfer owned by FTDIBUS.
     * Return a failure for this capability so UdeCx only supplies the buffer
     * form implemented by the endpoint callbacks.
     */
    if (RtlEqualMemory(
            CapabilityType,
            &GUID_USB_CAPABILITY_CHAINED_MDLS,
            sizeof(GUID))) {
        UNREFERENCED_PARAMETER(OutputBufferLength);
        UNREFERENCED_PARAMETER(OutputBuffer);
        return STATUS_NOT_SUPPORTED;
    }

    return STATUS_NOT_IMPLEMENTED;
}

NTSTATUS
M231EvtDeviceAdd(
    _In_ WDFDRIVER Driver,
    _Inout_ PWDFDEVICE_INIT DeviceInit
    )
{
    UNICODE_STRING deviceName;
    UNICODE_STRING symbolicLink;
    UNICODE_STRING hostReference;
    UNICODE_STRING securityDescriptor;
    WDF_OBJECT_ATTRIBUTES attributes;
    WDF_IO_QUEUE_CONFIG queueConfig;
    UDECX_WDF_DEVICE_CONFIG udeConfig;
    PDEVICE_CONTEXT context;
    WDFDEVICE device;
    NTSTATUS status;

    UNREFERENCED_PARAMETER(Driver);

    /* A ACL precisa ser aplicada antes de UdecxInitializeWdfDeviceInit, na
     * mesma ordem do controlador de referencia da Microsoft. */
    RtlInitUnicodeString(
        &securityDescriptor,
        /* O emulador e um processo normal do usuario logado.  Sem a entrada
         * BU o link \\DosDevices\\M231VirtualUsb devolve ERROR_ACCESS_DENIED,
         * portanto o IOCTL de plug-in nunca chega ao UdeCx. */
        L"D:P(A;;GA;;;SY)(A;;GRGWGX;;;BA)(A;;GRGW;;;WD)(A;;GR;;;RC)");
    status = WdfDeviceInitAssignSDDLString(DeviceInit, &securityDescriptor);
    if (!NT_SUCCESS(status)) {
        return status;
    }

    status = UdecxInitializeWdfDeviceInit(DeviceInit);
    if (!NT_SUCCESS(status)) {
        return status;
    }

    RtlInitUnicodeString(&deviceName, L"\\Device\\M231VirtualUsb");
    RtlInitUnicodeString(&symbolicLink, L"\\DosDevices\\M231VirtualUsb");
    status = WdfDeviceInitAssignName(DeviceInit, &deviceName);
    if (!NT_SUCCESS(status)) {
        return status;
    }

    WDF_OBJECT_ATTRIBUTES_INIT_CONTEXT_TYPE(&attributes, DEVICE_CONTEXT);
    status = WdfDeviceCreate(&DeviceInit, &attributes, &device);
    if (!NT_SUCCESS(status)) {
        return status;
    }
    status = WdfDeviceCreateSymbolicLink(device, &symbolicLink);
    if (!NT_SUCCESS(status)) {
        return status;
    }
    RtlInitUnicodeString(&hostReference, L"M231VirtualUsb");
    status = WdfDeviceCreateDeviceInterface(
        device,
        &GUID_DEVINTERFACE_USB_HOST_CONTROLLER,
        &hostReference);
    if (!NT_SUCCESS(status)) {
        return status;
    }

    UDECX_WDF_DEVICE_CONFIG_INIT(&udeConfig, M231EvtQueryUsbCapability);
    status = UdecxWdfDeviceAddUsbDeviceEmulation(device, &udeConfig);
    if (!NT_SUCCESS(status)) {
        return status;
    }

    context = DeviceGetContext(device);
    RtlZeroMemory(context, sizeof(*context));
    WDF_OBJECT_ATTRIBUTES_INIT(&attributes);
    attributes.ParentObject = device;
    status = WdfSpinLockCreate(&attributes, &context->BufferLock);
    if (!NT_SUCCESS(status)) {
        return status;
    }

    WDF_IO_QUEUE_CONFIG_INIT_DEFAULT_QUEUE(&queueConfig, WdfIoQueueDispatchSequential);
    queueConfig.EvtIoDeviceControl = M231EvtIoDeviceControl;
    queueConfig.PowerManaged = WdfFalse;
    return WdfIoQueueCreate(device, &queueConfig, WDF_NO_OBJECT_ATTRIBUTES, WDF_NO_HANDLE);
}

NTSTATUS
DriverEntry(
    _In_ PDRIVER_OBJECT DriverObject,
    _In_ PUNICODE_STRING RegistryPath
    )
{
    WDF_DRIVER_CONFIG config;
    WDF_DRIVER_CONFIG_INIT(&config, M231EvtDeviceAdd);
    return WdfDriverCreate(
        DriverObject,
        RegistryPath,
        WDF_NO_OBJECT_ATTRIBUTES,
        &config,
        WDF_NO_HANDLE);
}
