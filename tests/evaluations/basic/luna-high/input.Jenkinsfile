pipeline {
    agent any

    environment {
        APP_ENV = "ci"
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        stage('Build') {
            steps {
                sh './gradlew --no-daemon clean build'
            }
        }

        stage('Test') {
            steps {
                sh './gradlew --no-daemon test'
            }

            post {
                always {
                    junit 'build/test-results/test/*.xml'
                }
            }
        }
    }

    post {
        always {
            archiveArtifacts artifacts: 'build/libs/*.jar', fingerprint: true
        }
    }
}