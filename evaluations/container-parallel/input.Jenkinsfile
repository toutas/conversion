pipeline {
    agent {
        docker {
            image 'gradle:8.7.0-jdk17-alpine'
            args '-u root'
        }
    }

    environment {
        APP_ENV = "ci"
    }

    options {
        timestamps()
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        stage('Prepare Tools') {
            steps {
                sh 'apk add --no-cache bash git jq'
                sh 'jq --version'
            }
        }

        stage('Build') {
            steps {
                sh './gradlew --no-daemon clean build'
            }
        }

        stage('Tests') {
            parallel {
                stage('Unit Tests') {
                    steps {
                        sh './gradlew --no-daemon test'
                    }

                    post {
                        always {
                            junit 'build/test-results/test/*.xml'
                        }
                    }
                }

                stage('Lint') {
                    steps {
                        sh './gradlew --no-daemon check'
                    }
                }
            }
        }

        stage('Publish Artifacts') {
            when {
                branch 'main'
            }

            steps {
                echo 'Publishing artifacts (placeholder)'
            }
        }
    }

    post {
        always {
            archiveArtifacts artifacts: 'build/libs/*.jar', fingerprint: true
        }
    }
}