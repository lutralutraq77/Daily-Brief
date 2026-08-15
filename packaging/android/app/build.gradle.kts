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
        // versionCode must increase or Android refuses to install the new APK
        // over the old one -- it is the only field the installer compares.
        versionCode = 4
        versionName = "1.4.0"

        // ABIs are declared per flavour below, not here.
    }

    // One APK per ABI, via flavours rather than `splits`.
    //
    // `splits { abi { ... } }` is the usual way to do this and it CANNOT be used
    // with Chaquopy. The two constraints are mutually exclusive:
    //   AGP:      "ndk abiFilters cannot be present when splits abi filters are set"
    //   Chaquopy: "requires ndk.abiFilters: you may want to add it to defaultConfig"
    // A flavour gets its own abiFilters, which satisfies both.
    //
    // Why bother: Chaquopy ships a whole ~15 MB CPython runtime per ABI. One APK
    // carrying both meant 32.8% of it could not be dlopen'd on the device it was
    // installed on.
    //
    // The cost, stated plainly: the two APKs sum to more total build storage than
    // the single fat one did, because the ~12 MB they share is now duplicated.
    // That is the right trade when the user downloads one of them -- but it is
    // not free, and the build script must name the ABI in each filename or a
    // sideloader cannot tell which file is theirs.
    flavorDimensions += "abi"
    productFlavors {
        // arm64-v8a: every phone this is built for, including the GrapheneOS one.
        create("arm64") {
            dimension = "abi"
            ndk { abiFilters += "arm64-v8a" }
        }
        // x86_64: the emulator only. Kept because it is what the app is tested on.
        create("x86_64") {
            dimension = "abi"
            ndk { abiFilters += "x86_64" }
        }
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
    // material-icons-core only. The app draws six icons; material-icons-extended
    // dexed to 21.8 MiB (11,105 classes) to supply three of them, which is 4.0
    // MiB of the deflated APK and what pushed the build into a second dex file.
    // Place, Refresh and Delete come from core; Article, CalendarMonth and
    // GridView are vendored in Icons.kt with the library's own path data.
    // core is already on the classpath via material3 -- named here so that stays
    // true if material3 ever stops pulling it in.
    implementation("androidx.compose.material:material-icons-core")

    implementation("androidx.work:work-runtime-ktx:2.9.1")
}
