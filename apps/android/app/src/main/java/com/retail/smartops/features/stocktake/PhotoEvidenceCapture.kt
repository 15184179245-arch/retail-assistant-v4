package com.retail.smartops.features.stocktake

import com.retail.smartops.core.network.HttpResult
import com.retail.smartops.core.network.HttpTransport
import java.io.File
import java.net.URLEncoder
import java.security.MessageDigest
import java.util.UUID
import org.json.JSONObject

data class PhotoEvidence(
    val photoId: String = UUID.randomUUID().toString(),
    val localPath: String,
    val sha256: String,
    val sizeBytes: Long,
    val capturedAtMs: Long,
    val remoteMediaId: String? = null,
    val uploadState: String = "local_only", // "local_only", "uploaded", "failed"
    val lastError: String? = null,
)

object PhotoDigest {
    fun sha256(bytes: ByteArray): String {
        val md = MessageDigest.getInstance("SHA-256")
        return md.digest(bytes).joinToString("") { "%02x".format(it) }
    }
}

class PhotoEvidenceManager(
    private val storageDir: File? = null,
    private val clock: () -> Long = System::currentTimeMillis,
) {
    fun createEvidenceFromBytes(
        bytes: ByteArray,
        filename: String = "photo_${clock()}.jpg",
    ): PhotoEvidence {
        val hash = PhotoDigest.sha256(bytes)
        val targetFile = if (storageDir != null) {
            if (!storageDir.exists()) storageDir.mkdirs()
            val file = File(storageDir, "$hash-$filename")
            file.writeBytes(bytes)
            file
        } else {
            File("/tmp/$hash-$filename")
        }

        return PhotoEvidence(
            localPath = targetFile.absolutePath,
            sha256 = hash,
            sizeBytes = bytes.size.toLong(),
            capturedAtMs = clock(),
            uploadState = "local_only",
        )
    }

    /**
     * Upload photo to backend media service.
     * Acceptance guarantee: If upload fails, the local evidence and SHA-256 are fully preserved
     * with uploadState = 'failed', and never thrown to destroy the stocktake observation!
     */
    fun uploadPhoto(
        evidence: PhotoEvidence,
        transport: HttpTransport,
        token: String,
    ): PhotoEvidence {
        if (evidence.remoteMediaId != null && evidence.uploadState == "uploaded") {
            return evidence
        }

        val file = File(evidence.localPath)
        val bytes = if (file.exists()) file.readBytes() else ByteArray(0)
        val filename = URLEncoder.encode(file.name, "UTF-8")
        val path = "/api/v1/media/assets?filename=$filename&category=inventory_photo&source=mobile_stocktake"

        return try {
            val response = transport.post(path, String(bytes, Charsets.ISO_8859_1), token)
            if (response.status in 200..299) {
                val json = JSONObject(response.body)
                val data = json.optJSONObject("data")
                val mediaId = data?.optString("id") ?: evidence.photoId
                evidence.copy(
                    remoteMediaId = mediaId,
                    uploadState = "uploaded",
                    lastError = null,
                )
            } else {
                evidence.copy(
                    uploadState = "failed",
                    lastError = "HTTP ${response.status}: ${response.body}",
                )
            }
        } catch (e: Exception) {
            evidence.copy(
                uploadState = "failed",
                lastError = e.message ?: "Upload network failure",
            )
        }
    }
}
