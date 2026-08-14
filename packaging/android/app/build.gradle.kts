import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("com.chaquo.python")
}

// Optional release signing; without it the release build comes out unsigned.
val keystorePropsFile = rootProject.file("keystore.properties")
val keystoreProps = Properties().apply {
    if (keystorePropsFile.exists()) keystorePropsFile.inputStream().use { load(it) }
}
val hasReleaseKeystore = keystoreProps.getProperty("storeFile") != null

/**
 * The collectors are not vendored into this project -- they are copied from the
 * repository root at build time, so the Android app can never drift from the
 * desktop builds.
 *
 * Every top-level .py, not a hand-maintained list: a list silently goes stale
 * the moment a module is added, and the app then fails at runtime with
 * ModuleNotFoundError deep inside a collector.
 *
 * The glob is safe because it is limited to *.py at the top level. The secrets
 * in this repo are config.json and calendars.txt -- the latter holds a Google
 * secret iCal URL, a permanent bearer token -- and neither matches.
 */
val pythonModules = listOf("*.py")

val repoRoot = rootProject.projectDir.parentFile.parentFile

// Generated content goes under build/, not into src/. Writing into a source
// directory that another task consumes leaves Gradle unable to order the two,
// and it fails the build rather than guessing.
val generatedPythonDir = layout.buildDirectory.dir("generated/python")

val syncPythonSources by tasks.registering(Sync::class) {
    description = "Copy the shared Python collectors into the APK's Python source set."
    from(repoRoot) { include(pythonModules) }
    into(generatedPythonDir)
}

// Chaquopy's merge task reads the python source set directly, so it needs to be
// told who produces those files.
tasks.matching { it.name.matches(Regex("merge.*PythonSources")) }
    .configureEach { dependsOn(syncPythonSources) }

android {
    namespace = "dev.danny.dailybrief"
    compileSdk = 35

    defaultConfig {
        applicationId = "dev.danny.dailybrief"
        minSdk = 24
        targetSdk = 35
        versionCode = 1
        versionName = "1.1.0"

        // Chaquopy ships a CPython runtime per ABI. Two covers a real phone and
        // the emulator; all four would roughly double the APK for nothing.
        ndk { abiFilters += listOf("arm64-v8a", "x86_64") }
    }

    signingConfigs {
        if (hasReleaseKeystore) {
            create("release") {
                storeFile = rootProject.file(keystoreProps.getProperty("storeFile"))
                storePassword = keystoreProps.getProperty("storePassword")
                keyAlias = keystoreProps.getProperty("keyAlias")
                keyPassword = keystoreProps.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            if (hasReleaseKeystore) signingConfig = signingConfigs.getByName("release")
            // The Python is stored as assets, which R8 does not touch, and the
            // Kotlin shell is small. Shrinking buys little and risks Chaquopy's
            // reflection into the runtime.
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures {
        compose = true
        buildConfig = true
    }
    packaging {
        resources { excludes += setOf("/META-INF/{AL2.0,LGPL2.1}") }
    }
}

chaquopy {
    defaultConfig {
        // 3.12 or newer is required, not preferred: the collectors use PEP 701
        // f-strings (multi-line expressions, backslash escapes inside the
        // braces), which are a SyntaxError on 3.11 and earlier.
        version = "3.12"
        // Nothing to pip install: the collectors are standard library only,
        // which is what makes this port a packaging job rather than a rewrite.
    }
    sourceSets {
        getByName("main") { srcDir(generatedPythonDir.get().asFile) }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.7")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")

    implementation(platform("androidx.compose:compose-bom:2024.10.01"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")

    implementation("androidx.work:work-runtime-ktx:2.9.1")
}
