plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("org.jetbrains.kotlin.kapt")
}

android {
    namespace = "com.retail.smartops"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.retail.smartops"
        minSdk = 26
        targetSdk = 34
        versionCode = 2
        versionName = "0.2.0-dev"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures { compose = true; buildConfig = true }

    testOptions { unitTests.isIncludeAndroidResources = false }

    sourceSets {
        getByName("test") {
            resources.srcDirs("src/test/resources", file("../../../docs/ui"), file("../../../contracts"))
        }
    }

    buildTypes {
        debug {
            val debugOrigin = (project.findProperty("apiBaseUrl") as String?)
                ?: "http://10.0.2.2:8000"
            buildConfigField("String", "API_BASE_URL", "\"$debugOrigin\"")
            // AGT-018: optional isolated device-test package, debug only. Any value
            // that could collide with, or escape from, the production id is rejected.
            val deviceSuffix = (project.findProperty("deviceTestApplicationIdSuffix") as String?)
                ?.trim().orEmpty()
            if (deviceSuffix.isNotEmpty()) {
                require(Regex("^\\.[a-z][a-z0-9_]{2,30}(\\.[a-z][a-z0-9_]{2,30})*$").matches(deviceSuffix)) {
                    "deviceTestApplicationIdSuffix must look like .agt018"
                }
                applicationIdSuffix = deviceSuffix
            }
        }
        release {
            buildConfigField("String", "API_BASE_URL", "\"https://api.retail.example.invalid\"")
        }
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

dependencies {
    implementation(platform("androidx.compose:compose-bom:2024.09.03"))
    implementation("androidx.activity:activity-compose:1.9.2")
    implementation("androidx.navigation:navigation-compose:2.7.7")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    // HW-001: local CameraX analysis; bundled ML Kit decodes without a model download.
    implementation("androidx.camera:camera-core:1.3.4")
    implementation("androidx.camera:camera-camera2:1.3.4")
    implementation("androidx.camera:camera-lifecycle:1.3.4")
    implementation("androidx.camera:camera-view:1.3.4")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.6")
    implementation("com.google.mlkit:barcode-scanning:17.3.0")

    // MOB-004: app-private, scope-partitioned Room cache (no database destructive fallback).
    implementation("androidx.room:room-runtime:2.7.2")
    implementation("androidx.room:room-ktx:2.7.2")
    kapt("androidx.room:room-compiler:2.7.2")
    // MOB-002: WorkManager for offline sales draft replay and idempotency
    implementation("androidx.work:work-runtime-ktx:2.9.1")
    androidTestImplementation("androidx.room:room-testing:2.7.2")
    androidTestImplementation(platform("androidx.compose:compose-bom:2024.09.03"))
    androidTestImplementation("androidx.compose.ui:ui-test-junit4")
    debugImplementation("androidx.compose.ui:ui-test-manifest")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    androidTestImplementation("androidx.test:core:1.6.1")
    androidTestImplementation("androidx.test:runner:1.6.2")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}

kapt {
    arguments {
        arg("room.schemaLocation", "$projectDir/schemas")
    }
}
