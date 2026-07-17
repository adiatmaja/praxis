import os

for root, dirs, files in os.walk('tests'):
    for f in files:
        if f.endswith('.py'):
            path = os.path.join(root, f)
            with open(path, 'r') as file:
                content = file.read()
            
            new_content = content.replace('.auth_token ==', '.auth_token.get_secret_value() ==')
            new_content = new_content.replace('settings.auth_token,', 'settings.auth_token.get_secret_value(),')
            new_content = new_content.replace('test_settings.auth_token', 'test_settings.auth_token.get_secret_value()')
            
            if content != new_content:
                with open(path, 'w') as file:
                    file.write(new_content)
                print(f"Updated {path}")
