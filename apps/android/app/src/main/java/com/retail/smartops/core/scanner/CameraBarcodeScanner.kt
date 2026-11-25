package com.retail.smartops.core.scanner

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.core.ExperimentalGetImage
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.barcode.BarcodeScannerOptions
import com.google.mlkit.vision.common.InputImage
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/** Barcodes are UNTRUSTED search text, never a product or authorization identity. */
sealed interface ScanOutcome {
    data class Found(val rawValue: String, val format: Int) : ScanOutcome
    data class Multiple(val rawValues: List<String>) : ScanOutcome
    data object NoCode : ScanOutcome
    data object Cancelled : ScanOutcome
    data object PermissionDenied : ScanOutcome
    data object Failed : ScanOutcome
}

/** Pure selection contract; duplicate frames/duplicate codes cannot select a product. */
object ScanSelection {
    private const val MAX_LENGTH = 128

    fun choose(codes: List<Pair<String?, Int>>): ScanOutcome {
        val candidates = codes.mapNotNull { (raw, format) ->
            val value = raw?.takeIf {
                it.isNotEmpty() && it.length <= MAX_LENGTH &&
                    it.none { char -> Character.isISOControl(char) }
            }
            value?.let { it to format }
        }.distinctBy { it.first }
        return when {
            candidates.isEmpty() -> ScanOutcome.NoCode
            candidates.size > 1 -> ScanOutcome.Multiple(candidates.map { it.first })
            else -> ScanOutcome.Found(candidates.single().first, candidates.single().second)
        }
    }
}

/** Request permission only on a user tap; denial never disables typed search. */
@Composable
fun BarcodeScanEntry(onResult: (ScanOutcome) -> Unit) {
    val context = LocalContext.current
    var cameraOpen by remember { mutableStateOf(false) }
    val callback by rememberUpdatedState(onResult)
    val launcher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) cameraOpen = true
        else callback(ScanOutcome.PermissionDenied)
    }

    if (cameraOpen) {
        CameraBarcodeScanner(
            onResult = { result ->
                cameraOpen = false
                callback(result)
            },
            onCancel = {
                cameraOpen = false
                callback(ScanOutcome.Cancelled)
            }
        )
    } else {
        OutlinedButton(onClick = {
            if (ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) ==
                PackageManager.PERMISSION_GRANTED
            ) cameraOpen = true
            else launcher.launch(Manifest.permission.CAMERA)
        }) { Text("相机扫条码（仍须人工确认）") }
    }
}

/**
 * Real CameraX frame analysis + bundled ML Kit model.
 * A decoded frame only returns candidate text; caller must search and confirm.
 * ImageProxy is always closed after the asynchronous ML Kit task completes.
 */
@OptIn(ExperimentalGetImage::class)
@Composable
fun CameraBarcodeScanner(
    onResult: (ScanOutcome) -> Unit,
    onCancel: () -> Unit
) {
    val context = LocalContext.current
    val owner = LocalLifecycleOwner.current
    val callback by rememberUpdatedState(onResult)
    val previewView = remember(context) {
        PreviewView(context).apply {
            implementationMode = PreviewView.ImplementationMode.COMPATIBLE
        }
    }

    DisposableEffect(owner, previewView) {
        val alive = AtomicBoolean(true)
        val finished = AtomicBoolean(false)
        val worker = Executors.newSingleThreadExecutor()
        val options = BarcodeScannerOptions.Builder().setBarcodeFormats(
            Barcode.FORMAT_CODE_128,
            Barcode.FORMAT_CODE_39,
            Barcode.FORMAT_CODE_93,
            Barcode.FORMAT_EAN_13,
            Barcode.FORMAT_EAN_8,
            Barcode.FORMAT_UPC_A,
            Barcode.FORMAT_UPC_E,
            Barcode.FORMAT_ITF,
            Barcode.FORMAT_QR_CODE,
            Barcode.FORMAT_DATA_MATRIX
        ).build()
        val decoder = BarcodeScanning.getClient(options)
        val cameraFuture = ProcessCameraProvider.getInstance(context)
        var provider: ProcessCameraProvider? = null
        var preview: Preview? = null
        var analysis: ImageAnalysis? = null
        cameraFuture.addListener({
            if (alive.get()) {
                try {
                    val p = cameraFuture.get()
                    val livePreview = Preview.Builder().build()
                    val frames = ImageAnalysis.Builder()
                        .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                        .build()
                    livePreview.setSurfaceProvider(previewView.surfaceProvider)
                    frames.setAnalyzer(worker) { frame ->
                        val media = frame.image
                        if (!alive.get() || finished.get() || media == null) {
                            frame.close()
                        } else {
                            val image = InputImage.fromMediaImage(
                                media, frame.imageInfo.rotationDegrees
                            )
                            decoder.process(image)
                                .addOnSuccessListener { barcodes ->
                                    val outcome = ScanSelection.choose(
                                        barcodes.map { it.rawValue to it.format }
                                    )
                                    if (outcome != ScanOutcome.NoCode &&
                                        alive.get() && finished.compareAndSet(false, true)
                                    ) callback(outcome)
                                }
                                .addOnFailureListener {
                                    if (alive.get() && finished.compareAndSet(false, true))
                                        callback(ScanOutcome.Failed)
                                }
                                .addOnCompleteListener { frame.close() }
                        }
                    }
                    p.bindToLifecycle(owner, CameraSelector.DEFAULT_BACK_CAMERA,
                        livePreview, frames)
                    provider = p
                    preview = livePreview
                    analysis = frames
                } catch (_: Exception) {
                    if (alive.get() && finished.compareAndSet(false, true))
                        callback(ScanOutcome.Failed)
                }
            }
        }, ContextCompat.getMainExecutor(context))

        onDispose {
            alive.set(false)
            val p = preview
            val a = analysis
            if (p != null && a != null) provider?.unbind(p, a)
            decoder.close()
            worker.shutdown()
        }
    }

    Column(modifier = Modifier.fillMaxWidth().padding(vertical = 8.dp)) {
        AndroidView(factory = { previewView },
            modifier = Modifier.fillMaxWidth().height(300.dp))
        Text("对准条码，保持平稳；无法识别可取消后手动输入。")
        OutlinedButton(onClick = onCancel) { Text("取消扫码 / 手工输入") }
    }
}
