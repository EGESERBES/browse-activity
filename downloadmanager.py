# Copyright (C) 2007, One Laptop Per Child
# Copyright (C) 2009, Tomeu Vizoso, Lucian Branescu
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

import os
import logging
from gettext import gettext as _
import time
import tempfile

import gtk
import hulahop
import xpcom
from xpcom.nsError import *
from xpcom import components
from xpcom.components import interfaces
from xpcom.server.factory import Factory

from sugar.datastore import datastore
from sugar import profile
from sugar import mime
from sugar.graphics.alert import Alert, TimeoutAlert
from sugar.graphics.icon import Icon
from sugar.graphics import style
from sugar.activity import activity
# #3903 - this constant can be removed and assumed to be 1 when dbus-python
# 0.82.3 is the only version used
import dbus
if dbus.version >= (0, 82, 3):
    DBUS_PYTHON_TIMEOUT_UNITS_PER_SECOND = 1
else:
    DBUS_PYTHON_TIMEOUT_UNITS_PER_SECOND = 1000

NS_BINDING_ABORTED = 0x804b0002             # From nsNetError.h
NS_ERROR_SAVE_LINK_AS_TIMEOUT = 0x805d0020  # From nsURILoader.h

DS_DBUS_SERVICE = 'org.laptop.sugar.DataStore'
DS_DBUS_INTERFACE = 'org.laptop.sugar.DataStore'
DS_DBUS_PATH = '/org/laptop/sugar/DataStore'

_MIN_TIME_UPDATE = 5        # In seconds
_MIN_PERCENT_UPDATE = 10

_MAX_DELTA_CACHE_TIME = 86400  # In seconds

_active_downloads = []
_dest_to_window = {}


def can_quit():
    return len(_active_downloads) == 0


def num_downloads():
    return len(_active_downloads)


def remove_all_downloads():
    for download in _active_downloads:
        download.cancelable.cancel(NS_ERROR_FAILURE)
        if download.dl_jobject is not None:
            datastore.delete(download.dl_jobject.object_id)
        download.cleanup()

def remove_old_parts():
    temp_path = os.path.join(activity.get_activity_root(), 'instance')
    if os.path.exists(temp_path):
        for file in os.listdir(temp_path):
            file_full_path = os.path.join(temp_path, file)
            modification_time = os.path.getmtime(file_full_path)
            if(time.time() - modification_time > _MAX_DELTA_CACHE_TIME):
                logging.debug('removing %s' % file_full_path)
                os.remove(file_full_path)

class HelperAppLauncherDialog:
    _com_interfaces_ = interfaces.nsIHelperAppLauncherDialog

    def promptForSaveToFile(self, launcher, window_context,
                            default_file, suggested_file_extension,
                            force_prompt=False):
        file_class = components.classes['@mozilla.org/file/local;1']
        dest_file = file_class.createInstance(interfaces.nsILocalFile)

        if default_file:
            default_file = default_file.encode('utf-8', 'replace')
            base_name, extension = os.path.splitext(default_file)
        else:
            base_name = ''
            if suggested_file_extension:
                extension = '.' + suggested_file_extension
            else:
                extension = ''

        temp_path = os.path.join(activity.get_activity_root(), 'instance')
        if not os.path.exists(temp_path):
            os.makedirs(temp_path)
        fd, file_path = tempfile.mkstemp(dir=temp_path, prefix=base_name,
                                         suffix=extension)
        os.close(fd)
        os.chmod(file_path, 0644)
        dest_file.initWithPath(file_path)

        interface_id = interfaces.nsIInterfaceRequestor
        requestor = window_context.queryInterface(interface_id)
        dom_window = requestor.getInterface(interfaces.nsIDOMWindow)
        _dest_to_window[file_path] = dom_window

        return dest_file

    def show(self, launcher, context, reason):
        launcher.saveToDisk(None, False)
        return NS_OK


components.registrar.registerFactory('{64355793-988d-40a5-ba8e-fcde78cac631}',
                                     'Sugar Download Manager',
                                     '@mozilla.org/helperapplauncherdialog;1',
                                     Factory(HelperAppLauncherDialog))


class Download:
    _com_interfaces_ = interfaces.nsITransfer

    def init(self, source, target, display_name, mime_info, start_time,
             temp_file, cancelable):
        self._source = source
        self._mime_type = mime_info.MIMEType
        self._temp_file = temp_file
        self._target_file = target.queryInterface(interfaces.nsIFileURL).file
        self._display_name = display_name
        self.cancelable = cancelable
        self.datastore_deleted_handler = None

        self.dl_jobject = None
        self._object_id = None
        self._last_update_time = 0
        self._last_update_percent = 0
        self._stop_alert = None

        file_path = self._target_file.path.encode('utf-8', 'replace')
        dom_window = _dest_to_window[file_path]
        del _dest_to_window[file_path]

        view = hulahop.get_view_for_window(dom_window)
        logging.debug('Download.init dom_window: %r', dom_window)
        self._activity = view.get_toplevel()

        return NS_OK

    def onStatusChange(self, web_progress, request, status, message):
        logging.info('Download.onStatusChange(%r, %r, %r, %r)',
                     web_progress, request, status, message)

    def onStateChange(self, web_progress, request, state_flags, status):
        if state_flags & interfaces.nsIWebProgressListener.STATE_START:
            self._create_journal_object()
            self._object_id = self.dl_jobject.object_id

            alert = TimeoutAlert(9)
            alert.props.title = _('Download started')
            alert.props.msg = self._get_file_name()
            self._activity.add_alert(alert)
            alert.connect('response', self.__start_response_cb)
            alert.show()
            global _active_downloads
            _active_downloads.append(self)

        elif state_flags & interfaces.nsIWebProgressListener.STATE_STOP:
            if NS_FAILED(status):
                # download cancelled
                self.cleanup()
                return

            self._stop_alert = Alert()
            self._stop_alert.props.title = _('Download completed')
            self._stop_alert.props.msg = self._get_file_name()
            open_icon = Icon(icon_name='zoom-activity')
            self._stop_alert.add_button(gtk.RESPONSE_APPLY,
                                        _('Show in Journal'), open_icon)
            open_icon.show()
            ok_icon = Icon(icon_name='dialog-ok')
            self._stop_alert.add_button(gtk.RESPONSE_OK, _('Ok'), ok_icon)
            ok_icon.show()
            self._activity.add_alert(self._stop_alert)
            self._stop_alert.connect('response', self.__stop_response_cb)
            self._stop_alert.show()

            self.dl_jobject.metadata['title'] = self._get_file_name()
            self.dl_jobject.metadata['description'] = _('From: %s') \
                % self._source.spec
            self.dl_jobject.metadata['progress'] = '100'
            self.dl_jobject.file_path = self._target_file.path

            if self._mime_type in ['application/octet-stream',
                                   'application/x-zip']:
                sniffed_mime_type = mime.get_for_file(self._target_file.path)
                self.dl_jobject.metadata['mime_type'] = sniffed_mime_type

            if self._check_image_mime_type():
                self.dl_jobject.metadata['preview'] = self._get_preview_image()

            datastore.write(self.dl_jobject,
                            transfer_ownership=True,
                            reply_handler=self._internal_save_cb,
                            error_handler=self._internal_save_error_cb,
                            timeout=360 * DBUS_PYTHON_TIMEOUT_UNITS_PER_SECOND)

    def _check_image_mime_type(self):
        for pixbuf_format in gtk.gdk.pixbuf_get_formats():
            if self._mime_type in pixbuf_format['mime_types']:
                return True
        return False

    def _get_preview_image(self):
        preview_width, preview_height = style.zoom(300), style.zoom(225)

        pixbuf = gtk.gdk.pixbuf_new_from_file(self._target_file.path)
        width, height = pixbuf.get_width(), pixbuf.get_height()

        scale = 1
        if (width > preview_width) or (height > preview_height):
            scale_x = preview_width / float(width)
            scale_y = preview_height / float(height)
            scale = min(scale_x, scale_y)

        pixbuf2 = gtk.gdk.Pixbuf(gtk.gdk.COLORSPACE_RGB, \
                            pixbuf.get_has_alpha(), \
                            pixbuf.get_bits_per_sample(), \
                            preview_width, preview_height)
        pixbuf2.fill(style.COLOR_WHITE.get_int())

        margin_x = int((preview_width - (width * scale)) / 2)
        margin_y = int((preview_height - (height * scale)) / 2)

        pixbuf.scale(pixbuf2, margin_x, margin_y, \
                            preview_width - (margin_x * 2), \
                            preview_height - (margin_y * 2), \
                            margin_x, margin_y, scale, scale, \
                            gtk.gdk.INTERP_BILINEAR)

        preview_data = []

        def save_func(buf, data):
            data.append(buf)

        pixbuf2.save_to_callback(save_func, 'png', user_data=preview_data)
        preview_data = ''.join(preview_data)
        return dbus.ByteArray(preview_data)

    def __start_response_cb(self, alert, response_id):
        global _active_downloads
        if response_id is gtk.RESPONSE_CANCEL:
            logging.debug('Download Canceled')
            logging.debug('target_path=%r', self._target_file.path)
            self.cancelable.cancel(NS_ERROR_FAILURE)
            try:
                datastore.delete(self._object_id)
            except Exception:
                logging.exception('Object has been deleted already')
            self.cleanup()
            if self._stop_alert is not None:
                self._activity.remove_alert(self._stop_alert)

        self._activity.remove_alert(alert)

    def __stop_response_cb(self, alert, response_id):
        global _active_downloads
        if response_id is gtk.RESPONSE_APPLY:
            logging.debug('Start application with downloaded object')
            activity.show_object_in_journal(self._object_id)
        self._activity.remove_alert(alert)

    def cleanup(self):
        global _active_downloads
        if self in _active_downloads:
            _active_downloads.remove(self)

        if self.datastore_deleted_handler is not None:
            self.datastore_deleted_handler.remove()
            self.datastore_deleted_handler = None

        if os.path.isfile(self._target_file.path):
            os.remove(self._target_file.path)
        if os.path.isfile(self._target_file.path + '.part'):
            os.remove(self._target_file.path + '.part')

        if self.dl_jobject is not None:
            self.dl_jobject.destroy()
            self.dl_jobject = None

    def _internal_save_cb(self):
        self.cleanup()

    def _internal_save_error_cb(self, err):
        logging.error('Error saving activity object to datastore: %s', err)
        self.cleanup()

    def onProgressChange64(self, web_progress, request, cur_self_progress,
                           max_self_progress, cur_total_progress,
                           max_total_progress):
        percent = (cur_self_progress * 100) / max_self_progress

        if (time.time() - self._last_update_time) < _MIN_TIME_UPDATE and \
           (percent - self._last_update_percent) < _MIN_PERCENT_UPDATE:
            return

        self._last_update_time = time.time()
        self._last_update_percent = percent

        if percent < 100:
            self.dl_jobject.metadata['progress'] = str(percent)
            datastore.write(self.dl_jobject)

    def _get_file_name(self):
        if self._display_name:
            return self._display_name
        elif self._source.scheme == 'data':
            return 'Data URI'
        else:
            uri = self._source
            if uri == None:
                return ''
            cls = components.classes['@mozilla.org/intl/texttosuburi;1']
            texttosuburi = cls.getService(interfaces.nsITextToSubURI)
            path = texttosuburi.unEscapeURIForUI(uri.originCharset, uri.spec)
            location, file_name = os.path.split(path)
            return file_name

    def _create_journal_object(self):
        self.dl_jobject = datastore.create()
        self.dl_jobject.metadata['title'] = _('Downloading %s from \n%s.') % \
                (self._get_file_name(), self._source.spec)

        self.dl_jobject.metadata['progress'] = '0'
        self.dl_jobject.metadata['keep'] = '0'
        self.dl_jobject.metadata['buddies'] = ''
        self.dl_jobject.metadata['preview'] = ''
        self.dl_jobject.metadata['icon-color'] = \
                profile.get_color().to_string()
        self.dl_jobject.metadata['mime_type'] = self._mime_type
        self.dl_jobject.file_path = ''
        datastore.write(self.dl_jobject)

        bus = dbus.SessionBus()
        obj = bus.get_object(DS_DBUS_SERVICE, DS_DBUS_PATH)
        datastore_dbus = dbus.Interface(obj, DS_DBUS_INTERFACE)
        self.datastore_deleted_handler = datastore_dbus.connect_to_signal(
            'Deleted', self.__datastore_deleted_cb,
            arg0=self.dl_jobject.object_id)

    def __datastore_deleted_cb(self, uid):
        logging.debug('Downloaded entry has been deleted from the data'
                      ' store: %r', uid)
        global _active_downloads
        if self in _active_downloads:
            # TODO: Use NS_BINDING_ABORTED instead of NS_ERROR_FAILURE.
            self.cancelable.cancel(NS_ERROR_FAILURE)
            self.cleanup()


components.registrar.registerFactory('{23c51569-e9a1-4a92-adeb-3723db82ef7c}',
                                     'Sugar Download',
                                     '@mozilla.org/transfer;1',
                                     Factory(Download))


def save_link(url, text, owner_document):
    # Inspired on Firefox' browser/base/content/nsContextMenu.js:saveLink()

    cls = components.classes["@mozilla.org/network/io-service;1"]
    io_service = cls.getService(interfaces.nsIIOService)
    uri = io_service.newURI(url, None, None)
    channel = io_service.newChannelFromURI(uri)

    auth_prompt_callback = xpcom.server.WrapObject(
            _AuthPromptCallback(owner_document.defaultView),
            interfaces.nsIInterfaceRequestor)
    channel.notificationCallbacks = auth_prompt_callback

    channel.loadFlags = channel.loadFlags | \
        interfaces.nsIRequest.LOAD_BYPASS_CACHE | \
        interfaces.nsIChannel.LOAD_CALL_CONTENT_SNIFFERS

    # HACK: when we QI for nsIHttpChannel on objects that implement
    # just nsIChannel, pyxpcom gets confused trac #1029
    if uri.scheme == 'http':
        if _implements_interface(channel, interfaces.nsIHttpChannel):
            channel.referrer = io_service.newURI(owner_document.documentURI,
                                                 None, None)

    # kick off the channel with our proxy object as the listener
    listener = xpcom.server.WrapObject(
            _SaveLinkProgressListener(owner_document),
            interfaces.nsIStreamListener)
    channel.asyncOpen(listener, None)


def _implements_interface(obj, interface):
    try:
        obj.QueryInterface(interface)
        return True
    except xpcom.Exception, e:
        if e.errno == NS_NOINTERFACE:
            return False
        else:
            raise


class _AuthPromptCallback(object):
    _com_interfaces_ = interfaces.nsIInterfaceRequestor

    def __init__(self, dom_window):
        self._dom_window = dom_window

    def getInterface(self, uuid):
        if uuid in [interfaces.nsIAuthPrompt, interfaces.nsIAuthPrompt2]:
            cls = components.classes["@mozilla.org/embedcomp/window-watcher;1"]
            window_watcher = cls.getService(interfaces.nsIPromptFactory)
            return window_watcher.getPrompt(self._dom_window, uuid)
        return None


class _SaveLinkProgressListener(object):
    _com_interfaces_ = interfaces.nsIStreamListener

    """ an object to proxy the data through to
    nsIExternalHelperAppService.doContent, which will wait for the appropriate
    MIME-type headers and then prompt the user with a file picker
    """

    def __init__(self, owner_document):
        self._owner_document = owner_document
        self._external_listener = None

    def onStartRequest(self, request, context):
        if request.status != NS_OK:
            logging.error("Error downloading link")
            return

        class_name = '@mozilla.org/uriloader/external-helper-app-service;1'
        cls = components.classes[class_name]
        interface_id = interfaces.nsIExternalHelperAppService
        external_helper = cls.getService(interface_id)

        channel = request.QueryInterface(interfaces.nsIChannel)

        self._external_listener = \
            external_helper.doContent(channel.contentType, request,
                                      self._owner_document.defaultView, True)
        self._external_listener.onStartRequest(request, context)

    def onStopRequest(self, request, context, statusCode):
        self._external_listener.onStopRequest(request, context, statusCode)

    def onDataAvailable(self, request, context, inputStream, offset, count):
        self._external_listener.onDataAvailable(request, context, inputStream,
                                                offset, count)
