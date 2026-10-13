python3 manage.py migrate
python3 manage.py check

npm run build:editor
./make_style.sh
python3 manage.py collectstatic
python3 manage.py compilemessages
python3 manage.py compilejsi18n